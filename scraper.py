"""
프로토 축구 승무패 분석용 스크래퍼.

기본 배당은 FotMob 1X2(참고 배당)입니다. 베트맨은 --betman / --catch 로만 수집합니다.
"""
from playwright.sync_api import sync_playwright
from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KST = timezone(timedelta(hours=9))

CACHE_TTL_SEC = 180
H2H_CACHE_TTL_SEC = 3600
_CACHE = {'data': None, 'ts': 0.0}
_H2H_CACHE = {}
_SCRAPE_LOCK = threading.Lock()


def _chromium_args():
    args = [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--disable-software-rasterizer',
        '--disable-extensions',
        '--disable-background-networking',
        '--mute-audio',
        '--no-first-run',
    ]
    if os.environ.get('RENDER') or os.environ.get('LOW_MEMORY') == '1':
        args.append('--disable-features=Translate,BackForwardCache,AcceptCHFrame')
    return args


JSON_HEADERS = {
    'Content-Type': 'application/json;charset=UTF-8',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'X-Requested-With': 'XMLHttpRequest',
    'Origin': 'https://www.betman.co.kr',
}


def _launch_browser(playwright):
    args = _chromium_args() + [
        '--disable-blink-features=AutomationControlled',
        '--disable-infobars',
    ]
    headed = os.environ.get('BETMAN_HEADED') == '1'
    last_err = None
    for channel in ('chrome', 'msedge', None):
        kwargs = {
            'headless': not headed,
            'args': args,
            'ignore_default_args': ['--enable-automation'],
        }
        proxy = os.environ.get('BETMAN_PROXY')
        if proxy:
            kwargs['proxy'] = {'server': proxy}
        if channel:
            kwargs['channel'] = channel
        try:
            browser = playwright.chromium.launch(**kwargs)
            print(f'[SCRAPE] browser {channel or "chromium"} headed={headed}', flush=True)
            return browser
        except Exception as exc:
            last_err = exc
            print(f'[SCRAPE] launch {channel or "chromium"} 실패: {exc}', flush=True)
    raise RuntimeError(f'브라우저를 시작하지 못했습니다: {last_err}') from last_err


def _new_page(browser):
    context = browser.new_context(
        ignore_https_errors=True,
        user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
        ),
        viewport={'width': 1400, 'height': 900},
        locale='ko-KR',
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return context.new_page()


def _body_preview(text, limit=160):
    return ' '.join((text or '').split())[:limit]


def _is_waf(text):
    lowered = (text or '').lower()
    return (
        'webfirewall' in lowered
        or 'security policies have been blocked' in lowered
        or '웹방화벽' in (text or '')
    )


def _loads_json(text, url):
    raw = (text or '').strip().lstrip('\ufeff')
    if _is_waf(raw):
        raise RuntimeError(
            '베트맨 웹방화벽이 요청을 차단했습니다. 2~3분 뒤에 다시 올려 주세요.'
        )
    if not raw:
        raise RuntimeError(f'{url} 빈 응답')
    if raw[0] not in '{[':
        raise RuntimeError(f'{url} JSON 아님: {_body_preview(raw)}')
    try:
        return json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f'{url} JSON 파싱 실패: {exc}') from exc


def _post_json(page, url, payload, referer):
    headers = dict(JSON_HEADERS)
    headers['Referer'] = referer
    resp = page.request.post(
        url,
        data=json.dumps(payload),
        headers=headers,
        timeout=20000,
    )
    print(f'[SCRAPE] POST {url} -> {resp.status}', flush=True)
    if resp.status != 200:
        raise RuntimeError(f'{url} HTTP {resp.status}')
    return _loads_json(resp.text(), url)


def _goto_capture(page, goto_url, needle, timeout=35000):
    """페이지가 스스로 호출하는 API 응답만 가로챕니다. 직접 POST는 하지 않습니다."""
    captured = {}

    def on_response(resp):
        if needle not in (resp.url or ''):
            return
        try:
            captured['data'] = _loads_json(resp.text(), resp.url)
        except Exception as exc:
            captured['err'] = str(exc)

    page.on('response', on_response)
    try:
        page.goto(goto_url, timeout=timeout, wait_until='domcontentloaded')
        print(f'[SCRAPE] goto {goto_url} ok', flush=True)
        try:
            page.wait_for_load_state('networkidle', timeout=8000)
        except Exception:
            pass
        deadline = time.time() + 18
        while time.time() < deadline:
            if captured.get('data') is not None:
                break
            if captured.get('err') and '웹방화벽' in str(captured.get('err')):
                break
            page.wait_for_timeout(250)
    except Exception as exc:
        captured.setdefault('err', str(exc))
        print(f'[SCRAPE] goto 실패 {goto_url}: {exc}', flush=True)
    finally:
        try:
            page.remove_listener('response', on_response)
        except Exception:
            pass
    if captured.get('data') is not None:
        print(f'[SCRAPE] capture {needle} ok', flush=True)
        return captured['data']
    if captured.get('err'):
        print(f'[SCRAPE] capture {needle} {captured["err"]}', flush=True)
    else:
        print(f'[SCRAPE] capture {needle} 없음', flush=True)
    return None


def _gm_ts_from_html(page):
    try:
        html = page.content() or ''
    except Exception:
        return None
    found = re.findall(r'gmTs["\']?\s*[:=]\s*["\']?(\d{5,7})', html)
    if not found:
        return None
    return max(found, key=found.count)


def _warmup_betman(page):
    last_err = None
    for url in (
        'https://www.betman.co.kr/main/mainPage/gamebuy/proto.do',
        'https://www.betman.co.kr/',
    ):
        try:
            page.goto(url, timeout=25000, wait_until='domcontentloaded')
            print(f'[SCRAPE] goto {url} ok', flush=True)
            page.wait_for_timeout(500)
            return
        except Exception as exc:
            last_err = exc
            print(f'[SCRAPE] goto 실패 {url}: {exc}', flush=True)
            try:
                page.goto(url, timeout=20000, wait_until='commit')
                print(f'[SCRAPE] goto commit {url} ok', flush=True)
                return
            except Exception as commit_exc:
                last_err = commit_exc
    raise RuntimeError(
        '베트맨(betman.co.kr)에 접속하지 못했습니다. '
        f'상세: {last_err}'
    ) from last_err


def _gm_ts_from(data):
    for game in (data or {}).get('protoGames') or []:
        if game.get('gmId') == 'G101' and game.get('gmTs'):
            return game.get('gmTs')
    return None


def _fetch_round_ts(page):
    proto = 'https://www.betman.co.kr/main/mainPage/gamebuy/proto.do'
    data = _goto_capture(page, proto, 'inqCacheBuyAbleGameInfoList.do')
    gm_ts = _gm_ts_from(data) or _gm_ts_from_html(page)
    if gm_ts:
        print(f'[SCRAPE] gmTs {gm_ts}', flush=True)
        return gm_ts
    print('[SCRAPE] 회차 가로채기 실패, 기본 회차 사용', flush=True)
    return 260105


def _fetch_game_info(page, gm_ts):
    slip = (
        f'https://www.betman.co.kr/main/mainPage/gamebuy/gameSlip.do?gmId=G101&gmTs={gm_ts}'
    )
    data = _goto_capture(page, slip, 'gameInfoInq.do')
    if data and data.get('compSchedules'):
        return data
    raise RuntimeError(
        '베트맨이 자동화 요청을 막고 있습니다. '
        '지금 같은 명령을 반복하면 차단이 더 길어집니다. '
        '10~15분 쉰 뒤 한 번만 다시 실행하세요. '
        '그래도 막히면 PowerShell에서 $env:BETMAN_HEADED=1 을 켠 다음 실행하세요.'
    )


def fetch_betman_data(force=False):
    """
    프로토 승부식 축구 승무패 배당률을 수집합니다.

    Returns:
        dict: round_info, sale_end, matches, fetched_at, cached
    """
    now = time.time()
    if not force and _CACHE['data'] and (now - _CACHE['ts']) < CACHE_TTL_SEC:
        cached = dict(_CACHE['data'])
        cached['cached'] = True
        return cached

    with _SCRAPE_LOCK:
        now = time.time()
        if not force and _CACHE['data'] and (now - _CACHE['ts']) < CACHE_TTL_SEC:
            cached = dict(_CACHE['data'])
            cached['cached'] = True
            return cached
        result = _scrape_odds()
        if result and not result.get('h2h_ready'):
            _run_h2h_sync(result)
        return result


def _scrape_odds():
    if os.environ.get('ODDS_SOURCE') == 'betman' or '--betman' in sys.argv:
        return _scrape_betman_odds()
    return _scrape_fotmob_odds()


def _scrape_betman_odds():
    p = sync_playwright().start()
    browser = None
    result = None
    try:
        browser = _launch_browser(p)
        page = _new_page(browser)
        gm_ts = _fetch_round_ts(page)
        game_data = _fetch_game_info(page, gm_ts)
        result = parse_game_data(game_data)
        result['cached'] = False
        result['h2h_ready'] = False
        result['gm_ts'] = game_data.get('gmTs') or gm_ts
        _attach_points(result.get('matches') or [])
        _CACHE['data'] = result
        _CACHE['ts'] = time.time()
        print(f'[SCRAPE] matches {len(result.get("matches") or [])}', flush=True)
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        p.stop()

    return result or get_dummy_result()


def _run_h2h_sync(result):
    """같은 프로세스에서 상대전적을 이어서 수집합니다."""
    gm_ts = result.get('gm_ts') or 260105
    matches = result.get('matches') or []
    p = sync_playwright().start()
    browser = None
    try:
        browser = _launch_browser(p)
        page = _new_page(browser)
        _warmup_betman(page)
        _attach_h2h_streaks(page, matches, gm_ts)
        result['h2h_ready'] = True
    except Exception as exc:
        print('[WARN] 상대전적 수집 실패:', exc, flush=True)
        result['h2h_ready'] = True
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        p.stop()


def _atomic_write_json(path, data):
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp_path, path)


def dump_result(path):
    """별도 프로세스에서 배당을 먼저 저장한 뒤 상대전적을 이어 씁니다."""
    try:
        use_betman = os.environ.get('ODDS_SOURCE') == 'betman' or '--betman' in sys.argv
        if use_betman:
            result = _scrape_betman_odds()
        else:
            result = _scrape_fotmob_odds(
                include_h2h=False,
                progress=lambda payload: _atomic_write_json(path, payload),
            )
        _atomic_write_json(path, result)
        print(f'[DUMP] odds {len(result.get("matches") or [])}건 저장', flush=True)
        want_h2h = os.environ.get('BETMAN_H2H') == '1' or '--h2h' in sys.argv
        if not use_betman:
            want_h2h = os.environ.get('FOTMOB_H2H', '1') != '0' or '--h2h' in sys.argv
        if want_h2h and result.get('matches') and not result.get('h2h_ready'):
            if use_betman:
                _run_h2h_sync(result)
            else:
                _attach_fotmob_h2h(result.get('matches') or [])
                result['h2h_ready'] = True
            _atomic_write_json(path, result)
            print('[DUMP] h2h 저장', flush=True)
        else:
            result['h2h_ready'] = True
            print('[DUMP] 상대전적 생략. 필요하면 --h2h', flush=True)
            _atomic_write_json(path, result)
        return result
    except Exception as exc:
        tb = traceback.format_exc()
        print(tb, flush=True)
        _atomic_write_json(path, {
            'matches': [],
            'error': (str(exc) + '\n\n' + tb)[-2500:],
            'h2h_ready': True,
            'cached': False,
        })
        raise SystemExit(1)


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _implied_probs(win_allot, draw_allot, lose_allot):
    parts = []
    for odd in (win_allot, draw_allot, lose_allot):
        parts.append(1.0 / odd if odd > 0 else 0.0)
    total = sum(parts)
    if total <= 0:
        return 0.0, 0.0, 0.0
    return tuple(round(p / total * 100, 1) for p in parts)


def _analysis_signal(fav_side, w_pct, l_pct, implied_w, implied_l):
    if fav_side == '홈':
        fav_vote, dog_vote = w_pct, l_pct
        fav_imp, dog_imp = implied_w, implied_l
    elif fav_side == '원정':
        fav_vote, dog_vote = l_pct, w_pct
        fav_imp, dog_imp = implied_l, implied_w
    else:
        return '혼전', 'signal-even'

    if dog_vote >= fav_vote and dog_vote >= 40:
        return '역배 과몰입', 'signal-upset'
    if fav_vote - fav_imp >= 12:
        return '정배 과몰입', 'signal-crowd'
    if fav_vote >= 55:
        return '정배 쏠림', 'signal-fav'
    return '혼전', 'signal-even'


def _empty_h2h():
    return {
        'h2h_checked': False,
        'h2h_team': '',
        'h2h_side': '',
        'h2h_streak': 0,
        'h2h_form': '',
    }


def _empty_pts():
    return {
        'home_pts': None,
        'away_pts': None,
        'home_rank': None,
        'away_rank': None,
        'pts_source': '',
    }


def _as_int(value):
    try:
        if value is None or value == '':
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


FOTMOB_LEAGUE_HINTS = (
    (9080, ('k리그1', 'k리1', 'k league 1', 'kleague1', 'k-league 1')),
    (9116, ('k리그2', 'k리2', 'k league 2', 'kleague2', 'k-league 2')),
    (47, ('epl', 'premier league', '프리미어리그', '프리미어')),
    (48, ('championship', 'efl', '챔피언십')),
    (87, ('laliga', 'la liga', '라리가', '프리메라')),
    (140, ('laliga2', 'la liga 2', '라리가2', '세군다')),
    (54, ('bundesliga', '분데스리가', '분데스')),
    (146, ('2. bundesliga', '분데스리가2', '2부 분데스')),
    (55, ('serie a', '세리에a', '세리에 a')),
    (86, ('serie b', '세리에b')),
    (53, ('ligue 1', '리그앙', '리그1', '리그 1')),
    (110, ('ligue 2', '리그2')),
    (57, ('eredivisie', '에레디비시')),
    (223, ('j. league', 'j1', 'j리그', 'j리그1', 'j league')),
    (8974, ('j. league 2', 'j2', 'j리그2')),
    (113, ('a-league', 'a리그', 'aleague')),
    (268, ('brasileiro', '브라질', '세리에a 브라질', 'brazil serie a')),
    (112, ('liga profesional', '아르헨티나')),
    (230, ('liga mx', '리가mx', '멕시코')),
    (130, ('mls', '메이저리그사커')),
    (61, ('liga portugal', '포르투갈', '프리메이라')),
    (64, ('premiership', '스코틀랜드', '스코티시')),
    (40, ('first division a', '벨기에', '주필러')),
    (536, ('saudi pro league', '사우디')),
    (120, ('super league', '중국', '중초')),
    (67, ('allsvenskan', '알스벤스칸', '스웨덴')),
    (59, ('eliteserien', '엘리테세리엔', '노르웨이')),
    (46, ('superligaen', '덴마크')),
    (42, ('champions league', '챔피언스리그', 'ucl', 'uefa cl')),
    (73, ('europa league', '유로파리그', 'uel')),
)

FOTMOB_LEAGUE_LABELS = {
    9080: 'K리그1',
    9116: 'K리그2',
    47: 'EPL',
    48: '챔피언십',
    87: '라리가',
    140: '라리가2',
    54: '분데스',
    146: '분데스2',
    55: '세리에A',
    86: '세리에B',
    53: '리그앙',
    110: '리그2',
    57: '에레디비시',
    223: 'J리그',
    8974: 'J리그2',
    113: 'A리그',
    268: '브라질',
    112: '아르헨티나',
    230: '리가MX',
    130: 'MLS',
    61: '포르투갈',
    64: '스코틀랜드',
    40: '벨기에',
    536: '사우디',
    120: '중초',
    67: '스웨덴',
    59: '노르웨이',
    46: '덴마크',
    42: 'UCL',
    73: 'UEL',
}

# 스포츠토토 프로토 승부식에 자주 편성되는 리그만 수집.
# 사우디·분데스2·포르투갈·기타 2부/주변 리그는 제외.
PROTO_FOTMOB_LEAGUE_IDS = frozenset({
    9080, 9116,  # K리그1/2
    223, 8974,   # J리그1/2
    47, 48,      # EPL / Championship
    87,          # 라리가
    54,          # 분데스 1부
    55,          # 세리에A
    53,          # 리그앙
    57,          # 에레디비시
    113,         # A리그
    130,         # MLS
    42, 73,      # UCL / UEL
})

FOTMOB_ODDS_LEAGUE_IDS = PROTO_FOTMOB_LEAGUE_IDS

TEAM_HINTS = (
    ('맨체스터시티', 'manchester city'),
    ('맨체스터유나이티드', 'manchester united'),
    ('파리생제르맹', 'paris saint'),
    ('토트넘홋스퍼', 'tottenham'),
    ('바이에른뮌헨', 'bayern'),
    ('레알마드리드', 'real madrid'),
    ('아틀레티코마드리드', 'atletico'),
    ('인터밀란', 'inter'),
    ('보루시아도르트문트', 'dortmund'),
    ('바이엘레버쿠젠', 'leverkusen'),
    ('울산hd', 'ulsan'),
    ('울산현대', 'ulsan'),
    ('서울이랜드', 'eland'),
    ('충북청주', 'cheongju'),
    ('충남아산', 'asan'),
    ('김천상무', 'gimcheon'),
    ('수원삼성', 'suwon samsung'),
    ('수원fc', 'suwon fc'),
    ('전북현대', 'jeonbuk'),
    ('포항스틸러스', 'pohang'),
    ('제주sk', 'jeju'),
    ('대전하나', 'daejeon'),
    ('광주fc', 'gwangju'),
    ('대구fc', 'daegu'),
    ('인천유나이티드', 'incheon'),
    ('강원fc', 'gangwon'),
    ('fc서울', 'seoul'),
    ('부천fc', 'bucheon'),
    ('fc안양', 'anyang'),
    ('전남드래곤즈', 'jeonnam'),
    ('경남fc', 'gyeongnam'),
    ('부산아이파크', 'busan'),
    ('성남fc', 'seongnam'),
    ('김포fc', 'gimpo'),
    ('첼시', 'chelsea'),
    ('아스널', 'arsenal'),
    ('아스날', 'arsenal'),
    ('리버풀', 'liverpool'),
    ('토트넘', 'tottenham'),
    ('뉴캐슬', 'newcastle'),
    ('맨유', 'manchester united'),
    ('맨시티', 'manchester city'),
    ('바르셀로나', 'barcelona'),
    ('바르사', 'barcelona'),
    ('유벤투스', 'juventus'),
    ('밀란', 'milan'),
    ('나폴리', 'napoli'),
    ('로마', 'roma'),
    ('라치오', 'lazio'),
    ('아약스', 'ajax'),
    ('psv', 'psv'),
    ('페예노르트', 'feyenoord'),
    ('셀틱', 'celtic'),
    ('레인저스', 'rangers'),
    ('벤피카', 'benfica'),
    ('포르투', 'porto'),
    ('스포르팅', 'sporting'),
    ('울산', 'ulsan'),
    ('전북', 'jeonbuk'),
    ('서울', 'seoul'),
    ('인천', 'incheon'),
    ('대전', 'daejeon'),
    ('제주', 'jeju'),
    ('광주', 'gwangju'),
    ('대구', 'daegu'),
    ('포항', 'pohang'),
    ('수원', 'suwon'),
    ('김천', 'gimcheon'),
    ('강원', 'gangwon'),
    ('부천', 'bucheon'),
    ('청주', 'cheongju'),
    ('안양', 'anyang'),
    ('이랜드', 'eland'),
)

_FOTMOB = {'catalog': None, 'tables': {}}


def _fotmob_get(url, params=None):
    if params:
        url = url + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
            ),
            'Accept': 'application/json',
            'Referer': 'https://www.fotmob.com/',
            'Origin': 'https://www.fotmob.com',
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
        if not raw or raw.strip() in (b'null', b''):
            return None
        return json.loads(raw.decode('utf-8'))


def _norm_key(text):
    value = (text or '').lower()
    if value.startswith('fc'):
        value = value[2:]
    for junk in (
        '프로축구단', 'football club', 'futbol', 'calcio',
        'the ', ' fc', 'fc ', 'cf ', ' sc', ' afc', 'afc ',
    ):
        value = value.replace(junk, ' ')
    out = []
    for ch in value:
        if ch.isalnum() or '\uac00' <= ch <= '\ud7a3':
            out.append(ch)
    return ''.join(out)


def _fotmob_catalog():
    if _FOTMOB['catalog'] is not None:
        return _FOTMOB['catalog']
    items = []
    try:
        data = _fotmob_get('https://www.fotmob.com/api/data/allLeagues') or {}
    except Exception as exc:
        print(f'[FOTMOB] allLeagues 실패: {exc}', flush=True)
        _FOTMOB['catalog'] = []
        return _FOTMOB['catalog']
    groups = list(data.get('popular') or [])
    for pack in data.get('international') or []:
        groups.extend(pack.get('leagues') or [])
    for country in data.get('countries') or []:
        groups.extend(country.get('leagues') or [])
    for league in groups:
        if not isinstance(league, dict) or not league.get('id'):
            continue
        names = [
            str(league.get('name') or ''),
            str(league.get('localizedName') or ''),
        ]
        items.append((int(league['id']), names))
    _FOTMOB['catalog'] = items
    return items


def _resolve_fotmob_league(league_name):
    key = _norm_key(league_name)
    if not key:
        return None
    if key == 'pl':
        return 47
    ranked = []
    for league_id, hints in FOTMOB_LEAGUE_HINTS:
        for hint in hints:
            hk = _norm_key(hint)
            if not hk:
                continue
            if key == hk or hk in key or (len(key) >= 4 and key in hk):
                ranked.append((len(hk), league_id))
    if ranked:
        ranked.sort(reverse=True)
        return ranked[0][1]
    best = None
    best_len = 0
    for league_id, names in _fotmob_catalog():
        for name in names:
            nk = _norm_key(name)
            if len(nk) < 5:
                continue
            if nk == key or nk in key or key in nk:
                if len(nk) > best_len:
                    best = league_id
                    best_len = len(nk)
    return best


def _extract_fotmob_rows(data):
    rows = []
    for block in data.get('table') or []:
        inner = (block or {}).get('data') or {}
        table = inner.get('table') or {}
        if isinstance(table, dict):
            rows.extend(table.get('all') or [])
        for sub in inner.get('tables') or []:
            subtab = (sub or {}).get('table') or {}
            if isinstance(subtab, dict):
                rows.extend(subtab.get('all') or [])
            elif isinstance(subtab, list):
                rows.extend(subtab)
    uniq = {}
    ordered = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tid = row.get('id')
        key = tid if tid is not None else row.get('name')
        if key in uniq:
            continue
        uniq[key] = True
        ordered.append(row)
    return ordered


def _fotmob_table(league_id):
    cached = _FOTMOB['tables'].get(league_id)
    if cached is not None:
        return cached
    try:
        data = _fotmob_get(f'https://www.fotmob.com/api/data/leagues?id={league_id}') or {}
        rows = _extract_fotmob_rows(data)
        print(
            f"[FOTMOB] {league_id} {(data.get('details') or {}).get('name') or ''} "
            f"{len(rows)}팀",
            flush=True,
        )
    except Exception as exc:
        print(f'[FOTMOB] 리그 {league_id} 실패: {exc}', flush=True)
        rows = []
    _FOTMOB['tables'][league_id] = rows
    return rows


def _score_team(betman_name, row):
    raw = betman_name or ''
    compact = raw.replace(' ', '').lower()
    blob = _norm_key((row.get('name') or '') + ' ' + (row.get('shortName') or ''))
    bkey = _norm_key(raw)
    score = 0
    other_names = [_norm_key(row.get('name')), _norm_key(row.get('shortName'))]
    for other in other_names:
        if other and bkey and other == bkey:
            score = max(score, 120)
        elif other and bkey and (bkey in other or other in bkey) and min(len(bkey), len(other)) >= 4:
            score = max(score, 60)
    for ko, en in TEAM_HINTS:
        if ko not in compact:
            continue
        hk = _norm_key(en)
        if not hk or hk not in blob:
            continue
        bonus = 10 if blob == hk or blob.endswith(hk) else 0
        score = max(score, 50 + len(ko) * 8 + bonus)
    return score


def _match_fotmob_team(betman_name, rows):
    ranked = []
    for row in rows:
        score = _score_team(betman_name, row)
        if score >= 70:
            ranked.append((score, row))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 4:
        return None
    return ranked[0][1]


def _display_team_name(name):
    fake_row = {'name': name or '', 'shortName': name or ''}
    ranked = []
    for ko, _en in TEAM_HINTS:
        if not any('\uac00' <= ch <= '\ud7a3' for ch in ko):
            continue
        score = _score_team(ko, fake_row)
        if score >= 70:
            ranked.append((score, ko))
    if not ranked:
        return name or ''
    ranked.sort(key=lambda item: item[0], reverse=True)
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 4:
        return name or ''
    return ranked[0][1]


def _league_label(league_id, fallback=''):
    return FOTMOB_LEAGUE_LABELS.get(int(league_id or 0), fallback or str(league_id))


def _parse_utc(value):
    text = str(value or '').strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _format_kst(utc_value):
    dt = _parse_utc(utc_value)
    if not dt:
        return '', None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(KST)
    return local.strftime('%m/%d %H:%M'), local


def _extract_1x2(payload):
    if not isinstance(payload, dict):
        return None
    odds = payload.get('odds') or {}
    markets = list(odds.get('matchfactMarkets') or [])
    resolved = odds.get('resolvedOddsMarket')
    if isinstance(resolved, dict):
        markets.append(resolved)
    for market in markets:
        if not isinstance(market, dict):
            continue
        mapping = {}
        for sel in market.get('selections') or []:
            if not isinstance(sel, dict):
                continue
            name = str(sel.get('name') or sel.get('team') or '').strip().lower()
            val = _safe_float(
                sel.get('oddsDecimal') or sel.get('odds') or sel.get('coeff')
            )
            if val <= 1:
                continue
            if name in ('1', 'h', 'home'):
                mapping['win'] = val
            elif name in ('x', 'd', 'draw'):
                mapping['draw'] = val
            elif name in ('2', 'a', 'away'):
                mapping['lose'] = val
        if mapping.get('win', 0) > 1 and mapping.get('lose', 0) > 1:
            mapping.setdefault('draw', 0.0)
            return mapping
    return None


def _fotmob_match_odds(match_id):
    last_err = None
    for ccode in ('GBR', 'SWE', 'AUS'):
        try:
            payload = _fotmob_get(
                'https://www.fotmob.com/api/data/matchOdds',
                {
                    'matchId': str(match_id),
                    'ccode3': ccode,
                    'oddsFormat': 'decimal',
                    'client': 'web',
                },
            )
        except Exception as exc:
            last_err = exc
            continue
        extracted = _extract_1x2(payload)
        if extracted:
            return extracted
    if last_err:
        print(f'[FOTMOB] odds {match_id} 실패: {last_err}', flush=True)
    return None


def _decorate_odds(home, away, win_allot, draw_allot, lose_allot, w_pct=0.0, d_pct=0.0, l_pct=0.0):
    if win_allot > 0 and lose_allot > 0:
        if win_allot < lose_allot:
            fav_team, fav_side = home, '홈'
        elif lose_allot < win_allot:
            fav_team, fav_side = away, '원정'
        else:
            fav_team, fav_side = '-', '동일'
    else:
        fav_team, fav_side = '-', '-'
    implied_w, implied_d, implied_l = _implied_probs(win_allot, draw_allot, lose_allot)
    if w_pct <= 0 and d_pct <= 0 and l_pct <= 0:
        w_pct, d_pct, l_pct = implied_w, implied_d, implied_l
    signal, signal_class = _analysis_signal(fav_side, w_pct, l_pct, implied_w, implied_l)
    return {
        'win_allot': round(win_allot, 2),
        'draw_allot': round(draw_allot, 2),
        'lose_allot': round(lose_allot, 2),
        'fav_team': fav_team,
        'fav_side': fav_side,
        'w_pct': w_pct,
        'd_pct': d_pct,
        'l_pct': l_pct,
        'implied_w': implied_w,
        'implied_d': implied_d,
        'implied_l': implied_l,
        'signal': signal,
        'signal_class': signal_class,
    }


def _fotmob_days():
    try:
        return max(1, min(8, int(os.environ.get('FOTMOB_DAYS') or 5)))
    except (TypeError, ValueError):
        return 5


def _collect_fotmob_fixtures():
    days = _fotmob_days()
    start = datetime.now(KST).date()
    fixtures = []
    seen = set()
    for offset in range(days):
        day = (start + timedelta(days=offset)).strftime('%Y%m%d')
        try:
            payload = _fotmob_get(f'https://www.fotmob.com/api/data/matches?date={day}')
        except Exception as exc:
            print(f'[FOTMOB] matches {day} 실패: {exc}', flush=True)
            continue
        for league in (payload or {}).get('leagues') or []:
            try:
                league_id = int(league.get('id') or 0)
            except (TypeError, ValueError):
                continue
            if league_id not in FOTMOB_ODDS_LEAGUE_IDS:
                continue
            league_name = _league_label(league_id, league.get('name') or '')
            for raw in league.get('matches') or []:
                status = raw.get('status') or {}
                if status.get('cancelled') or status.get('finished') or status.get('started'):
                    continue
                match_id = raw.get('id')
                if not match_id or match_id in seen:
                    continue
                seen.add(match_id)
                home = (raw.get('home') or {}).get('name') or (raw.get('home') or {}).get('longName') or ''
                away = (raw.get('away') or {}).get('name') or (raw.get('away') or {}).get('longName') or ''
                utc = status.get('utcTime') or raw.get('time')
                date_text, local_dt = _format_kst(utc)
                fixtures.append({
                    'match_id': match_id,
                    'league_id': league_id,
                    'league': league_name,
                    'home_raw': home,
                    'away_raw': away,
                    'home_id': str((raw.get('home') or {}).get('id') or ''),
                    'away_id': str((raw.get('away') or {}).get('id') or ''),
                    'date': date_text,
                    'kickoff': local_dt,
                    'utc': utc,
                })
        time.sleep(0.06)
    fixtures.sort(key=lambda item: item.get('kickoff') or datetime.max.replace(tzinfo=KST))
    print(f'[FOTMOB] 일정 {days}일 {len(fixtures)}경기', flush=True)
    return fixtures


def _sort_matches(matches):
    matches.sort(key=lambda m: (
        str(m.get('date') or ''),
        str(m.get('league') or ''),
        str(m.get('home') or ''),
    ))
    return matches


def _fotmob_payload(matches, h2h_ready=False):
    _sort_matches(matches)
    if matches:
        round_info = f"{matches[0]['date'].split()[0]}~{matches[-1]['date'].split()[0]}"
        sale_end = matches[-1]['date']
    else:
        round_info = 'FotMob'
        sale_end = '-'
    return {
        'round_info': round_info,
        'sale_end': sale_end,
        'matches': matches,
        'fetched_at': datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'),
        'cached': False,
        'h2h_ready': h2h_ready,
        'odds_source': 'fotmob',
    }


def _scrape_fotmob_odds(include_h2h=True, progress=None):
    fixtures = _collect_fotmob_fixtures()
    matches = []
    odds_ok = 0

    def emit(h2h_ready=False):
        payload = _fotmob_payload(matches, h2h_ready=h2h_ready)
        _CACHE['data'] = payload
        _CACHE['ts'] = time.time()
        if progress:
            progress(payload)
        return payload

    for item in fixtures:
        time.sleep(0.06)
        extracted = _fotmob_match_odds(item['match_id'])
        if not extracted:
            continue
        home = _display_team_name(item['home_raw'])
        away = _display_team_name(item['away_raw'])
        decorated = _decorate_odds(
            home, away, extracted['win'], extracted['draw'], extracted['lose']
        )
        matches.append({
            'date': item['date'],
            'league': item['league'],
            'league_code': str(item['league_id']),
            'fotmob_league_id': item['league_id'],
            'home': home,
            'away': away,
            'home_id': item['home_id'],
            'away_id': item['away_id'],
            'total_bet': 0,
            'match_seq': item['match_id'],
            'odds_source': 'fotmob',
            **decorated,
            **_empty_h2h(),
            **_empty_pts(),
        })
        odds_ok += 1
        if odds_ok in (3, 8) or odds_ok % 12 == 0:
            emit(False)
    print(f'[FOTMOB] 배당 {odds_ok}/{len(fixtures)}경기', flush=True)

    result = emit(False)
    if not matches:
        last_dt = next((item.get('kickoff') for item in reversed(fixtures) if item.get('kickoff')), None)
        result['sale_end'] = last_dt.strftime('%Y-%m-%d %H:%M') if last_dt else '-'
    _attach_points(matches)
    result = emit(False)
    if include_h2h:
        _attach_fotmob_h2h(matches)
        result = emit(True)
    print(f'[SCRAPE] fotmob matches {len(matches)}', flush=True)
    return result


def _fotmob_h2h_past(details, home_id, away_id):
    raw_matches = ((details.get('content') or {}).get('h2h') or {}).get('matches') or []
    finished = []
    for raw in raw_matches:
        status = raw.get('status') or {}
        if not status.get('finished'):
            continue
        score = str(status.get('scoreStr') or '')
        parts = score.replace('–', '-').replace(':', '-').split('-')
        if len(parts) < 2:
            continue
        try:
            home_score = int(parts[0].strip())
            away_score = int(parts[1].strip())
        except (TypeError, ValueError):
            continue
        hid = str((raw.get('home') or {}).get('id') or '')
        aid = str((raw.get('away') or {}).get('id') or '')
        if home_score > away_score:
            winner = hid
        elif away_score > home_score:
            winner = aid
        else:
            winner = None
        utc = status.get('utcTime') or ((raw.get('time') or {}).get('utcTime') or '')
        finished.append({'winner': winner, 'utc': utc})
    finished.sort(key=lambda item: item['utc'] or '', reverse=True)
    return finished[:5]


def _analyze_fotmob_h2h(past, home_id, away_id, home_name, away_name):
    result = _empty_h2h()
    home_form = []
    for item in past:
        winner = item.get('winner')
        if winner is None:
            home_form.append('무')
        elif str(winner) == str(home_id):
            home_form.append('승')
        else:
            home_form.append('패')
    result['h2h_form'] = ''.join(home_form)

    def streak_of(team_id):
        count = 0
        for item in past:
            winner = item.get('winner')
            if winner is not None and str(winner) == str(team_id):
                count += 1
            else:
                break
        return count

    home_streak = streak_of(home_id)
    away_streak = streak_of(away_id)
    if home_streak >= 3:
        result.update(
            h2h_checked=True,
            h2h_team=home_name,
            h2h_side='홈',
            h2h_streak=home_streak,
        )
    elif away_streak >= 3:
        result.update(
            h2h_checked=True,
            h2h_team=away_name,
            h2h_side='원정',
            h2h_streak=away_streak,
        )
    return result


def _attach_fotmob_h2h(matches):
    checked = 0
    for match in matches:
        match.update(_empty_h2h())
        match_id = match.get('match_seq')
        if not match_id:
            continue
        try:
            time.sleep(0.06)
            details = _fotmob_get(
                'https://www.fotmob.com/api/data/matchDetails',
                {'matchId': str(match_id)},
            )
        except Exception as exc:
            print(f'[FOTMOB] h2h {match_id} 실패: {exc}', flush=True)
            continue
        past = _fotmob_h2h_past(details or {}, match.get('home_id'), match.get('away_id'))
        analyzed = _analyze_fotmob_h2h(
            past,
            match.get('home_id'),
            match.get('away_id'),
            match.get('home'),
            match.get('away'),
        )
        match.update(analyzed)
        if analyzed.get('h2h_checked'):
            checked += 1
    print(f'상대전적 수집(FotMob): {len(matches)}경기 중 3연승 이상 {checked}건', flush=True)


def _row_pts(row):
    pts = _as_int(row.get('pts'))
    rank = _as_int(row.get('idx') if row.get('idx') is not None else row.get('rank'))
    return {'pts': pts, 'rank': rank, 'name': row.get('name') or ''}


def _attach_points(matches):
    """FotMob 리그 순위표에서 승점·순위를 붙입니다."""
    grouped = {}
    for match in matches:
        match.update(_empty_pts())
        grouped.setdefault(match.get('league') or '-', []).append(match)

    filled = 0
    for league, group in grouped.items():
        league_id = group[0].get('fotmob_league_id') or _resolve_fotmob_league(league)
        if not league_id:
            print(f'[FOTMOB] 리그 매칭 실패: {league}', flush=True)
            continue
        time.sleep(0.15)
        rows = _fotmob_table(league_id)
        if not rows:
            continue
        row_by_id = {str(row.get('id')): row for row in rows if row.get('id') is not None}
        league_filled = 0
        for match in group:
            home = row_by_id.get(str(match.get('home_id') or '')) or _match_fotmob_team(match.get('home'), rows)
            away = row_by_id.get(str(match.get('away_id') or '')) or _match_fotmob_team(match.get('away'), rows)
            if home:
                info = _row_pts(home)
                match['home_pts'] = info['pts']
                match['home_rank'] = info['rank']
            if away:
                info = _row_pts(away)
                match['away_pts'] = info['pts']
                match['away_rank'] = info['rank']
            if home and away:
                match['pts_source'] = 'fotmob'
                league_filled += 1
        filled += league_filled
        print(f'[FOTMOB] {league} {league_filled}/{len(group)}경기', flush=True)
    print(f'승점 수집(FotMob): {len(matches)}경기 중 {filled}건', flush=True)


def _h2h_winner_id(el):
    binder = el.get('firstMatchBinder') or {}
    score = binder.get('score') or {}
    try:
        home_score = int(score.get('homeScore'))
        away_score = int(score.get('awayScore'))
    except (TypeError, ValueError):
        def gaining(team):
            latest = (
                ((team or {}).get('recordCollector') or {})
                .get('recordHolder') or {}
            ).get('Latest') or [{}]
            return latest[0].get('gainingScore')

        home_score = gaining(binder.get('home'))
        away_score = gaining(binder.get('away'))
        try:
            home_score = int(home_score)
            away_score = int(away_score)
        except (TypeError, ValueError):
            return None

    if home_score > away_score:
        return (binder.get('home') or {}).get('id')
    if away_score > home_score:
        return (binder.get('away') or {}).get('id')
    return None


def analyze_h2h_streak(schedule, home_id, away_id, home_name, away_name):
    """최근 5경기 상대전적에서 현재 3연승 이상인 팀을 찾습니다."""
    winners = []
    home_form = []
    for el in (schedule or [])[:5]:
        winner = _h2h_winner_id(el)
        winners.append(winner)
        if winner is None:
            home_form.append('무')
        elif str(winner) == str(home_id):
            home_form.append('승')
        else:
            home_form.append('패')

    def streak_of(team_id):
        count = 0
        for winner in winners:
            if winner is not None and str(winner) == str(team_id):
                count += 1
            else:
                break
        return count

    result = _empty_h2h()
    result['h2h_form'] = ''.join(home_form)
    home_streak = streak_of(home_id)
    away_streak = streak_of(away_id)
    if home_streak >= 3:
        result.update(
            h2h_checked=True,
            h2h_team=home_name,
            h2h_side='홈',
            h2h_streak=home_streak,
        )
    elif away_streak >= 3:
        result.update(
            h2h_checked=True,
            h2h_team=away_name,
            h2h_side='원정',
            h2h_streak=away_streak,
        )
    return result


def _h2h_cache_key(home_id, away_id):
    return tuple(sorted((str(home_id), str(away_id))))


def _fetch_confrontation(page, match, gm_ts):
    payload = {
        'gmId': 'G101',
        'gmTs': str(gm_ts),
        'matchSeq': str(match.get('match_seq') or ''),
        'gmType': 'SC',
        'saleYear': '',
        'gmOsidTs': '',
        'winOdds': str(match.get('win_allot') or ''),
        'drawOdds': str(match.get('draw_allot') or ''),
        'loseOdds': str(match.get('lose_allot') or ''),
        'league': match.get('league_code') or '',
        'teamId1': match.get('home_id') or '',
        'teamId2': match.get('away_id') or '',
        'homeAway': 0,
        'lastCount': 5,
        '_sbmInfo': {'_sbmInfo': {'debugMode': 'false'}},
    }
    resp = page.request.post(
        'https://www.betman.co.kr/gameinfo/inqConFrontationRecord.do',
        data=json.dumps(payload),
        headers={
            'Content-Type': 'application/json;charset=UTF-8',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': 'https://www.betman.co.kr',
            'Referer': (
                f'https://www.betman.co.kr/main/mainPage/gamebuy/'
                f'gameSlip.do?gmId=G101&gmTs={gm_ts}'
            ),
        },
        timeout=20000,
    )
    if resp.status != 200:
        return []
    try:
        data = resp.json()
    except Exception:
        return []
    return data.get('latestMatchSchedule') or []


def _attach_h2h_streaks(page, matches, gm_ts):
    """같은 브라우저 세션으로 경기별 최근 5경기 상대전적을 붙입니다."""
    checked = 0
    now = time.time()
    for match in matches:
        match.update(_empty_h2h())
        home_id = match.get('home_id')
        away_id = match.get('away_id')
        if not home_id or not away_id:
            continue

        cache_key = _h2h_cache_key(home_id, away_id)
        cached = _H2H_CACHE.get(cache_key)
        if cached and (now - cached['ts']) < H2H_CACHE_TTL_SEC:
            schedule = cached['schedule']
        else:
            schedule = _fetch_confrontation(page, match, gm_ts)
            _H2H_CACHE[cache_key] = {'ts': now, 'schedule': schedule}

        analyzed = analyze_h2h_streak(
            schedule, home_id, away_id, match.get('home'), match.get('away')
        )
        match.update(analyzed)
        if analyzed['h2h_checked']:
            checked += 1

    print(f'상대전적 수집: {len(matches)}경기 중 3연승 이상 {checked}건')


def parse_game_data(game_data):
    """gameInfoInq.do 응답에서 축구 풀타임 승무패만 추출합니다."""
    result = {
        'round_info': '',
        'sale_end': '',
        'matches': [],
        'fetched_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    cl = game_data.get('currentLottery') or {}
    result['round_info'] = cl.get('gameName') or str(game_data.get('gmTs') or '-')

    sale_end_ts = cl.get('saleEndDate')
    if sale_end_ts:
        result['sale_end'] = datetime.fromtimestamp(sale_end_ts / 1000).strftime('%Y-%m-%d %H:%M')

    comp = game_data.get('compSchedules') or {}
    keys = comp.get('keys') or []
    datas = comp.get('datas') or []
    idx = {k: i for i, k in enumerate(keys)}

    vote_map = {}
    for v in game_data.get('voteStatus') or []:
        seq = v.get('GM_SEQ')
        if seq is not None:
            vote_map[seq] = v

    matches = []
    for row in datas:
        try:
            item_code = str(row[idx.get('itemCode', 0)] or '')
            bet_typ_id = str(row[idx.get('betTypId', 40)] or '')
            bet_nm = str(row[idx.get('betNm', 39)] or '')

            # 축구 + 승무패 + 풀타임만 (전반 승무패 제외)
            if item_code != 'SC' or bet_typ_id != '1':
                continue
            if bet_nm and bet_nm != '축구 승무패':
                continue

            win_allot = _safe_float(row[idx.get('winAllot', 18)])
            draw_allot = _safe_float(row[idx.get('drawAllot', 20)])
            lose_allot = _safe_float(row[idx.get('loseAllot', 22)])
            if win_allot <= 0 and lose_allot <= 0:
                continue

            game_date_ts = row[idx.get('gameDate', 3)]
            game_date = (
                datetime.fromtimestamp(game_date_ts / 1000).strftime('%m/%d %H:%M')
                if game_date_ts else ''
            )

            league = row[idx.get('leagueShortName', 8)] or ''
            home = row[idx.get('homeName', 15)] or ''
            away = row[idx.get('awayName', 16)] or ''
            match_seq = row[idx.get('matchSeq', 12)]

            vote = vote_map.get(match_seq, {})
            w_bet = vote.get('W_BET_CNT', 0) or 0
            d_bet = vote.get('D_BET_CNT', 0) or 0
            l_bet = vote.get('L_BET_CNT', 0) or 0
            total_bet = w_bet + d_bet + l_bet

            if total_bet > 0:
                w_pct = round(w_bet / total_bet * 100, 1)
                d_pct = round(d_bet / total_bet * 100, 1)
                l_pct = round(l_bet / total_bet * 100, 1)
            else:
                w_pct = d_pct = l_pct = 0.0

            if win_allot > 0 and lose_allot > 0:
                if win_allot < lose_allot:
                    fav_team, fav_side = home, '홈'
                elif lose_allot < win_allot:
                    fav_team, fav_side = away, '원정'
                else:
                    fav_team, fav_side = '-', '동일'
            else:
                fav_team, fav_side = '-', '-'

            implied_w, implied_d, implied_l = _implied_probs(win_allot, draw_allot, lose_allot)
            signal, signal_class = _analysis_signal(
                fav_side, w_pct, l_pct, implied_w, implied_l
            )

            matches.append({
                'date': game_date,
                'league': league,
                'league_code': row[idx.get('leagueCode', 6)] or '',
                'home': home,
                'away': away,
                'home_id': row[idx.get('homeId', 13)] or '',
                'away_id': row[idx.get('awayId', 14)] or '',
                'win_allot': win_allot,
                'draw_allot': draw_allot,
                'lose_allot': lose_allot,
                'fav_team': fav_team,
                'fav_side': fav_side,
                'w_pct': w_pct,
                'd_pct': d_pct,
                'l_pct': l_pct,
                'total_bet': total_bet,
                'match_seq': match_seq,
                'implied_w': implied_w,
                'implied_d': implied_d,
                'implied_l': implied_l,
                'signal': signal,
                'signal_class': signal_class,
                **_empty_h2h(),
                **_empty_pts(),
            })
        except (IndexError, KeyError, TypeError):
            continue

    matches.sort(key=lambda x: x['date'])
    result['matches'] = matches
    return result


def get_dummy_result():
    """크롤링 실패 시 화면 확인용 샘플."""
    return {
        'round_info': '테스트',
        'sale_end': '데이터 없음',
        'cached': False,
        'h2h_ready': True,
        'matches': [
            {
                'date': '09/06 19:00', 'league': 'K리그1', 'home': '울산HD', 'away': 'FC서울',
                'win_allot': 1.45, 'draw_allot': 3.50, 'lose_allot': 4.20,
                'fav_team': '울산HD', 'fav_side': '홈',
                'w_pct': 65.2, 'd_pct': 18.3, 'l_pct': 16.5, 'total_bet': 5000, 'match_seq': 0,
                'implied_w': 62.0, 'implied_d': 25.7, 'implied_l': 12.3,
                'signal': '정배 쏠림', 'signal_class': 'signal-fav',
                'home_id': 'K01', 'away_id': 'K09', 'league_code': 'SC001',
                'h2h_checked': True, 'h2h_team': '울산HD', 'h2h_side': '홈',
                'h2h_streak': 4, 'h2h_form': '승승승승무',
                'home_pts': 52, 'away_pts': 48, 'home_rank': 2, 'away_rank': 4,
                'pts_source': 'fotmob',
            },
            {
                'date': '09/06 19:00', 'league': 'K리그1', 'home': '전북', 'away': '인천',
                'win_allot': 1.80, 'draw_allot': 3.20, 'lose_allot': 3.40,
                'fav_team': '전북', 'fav_side': '홈',
                'w_pct': 52.1, 'd_pct': 24.0, 'l_pct': 23.9, 'total_bet': 3200, 'match_seq': 0,
                'implied_w': 48.0, 'implied_d': 27.0, 'implied_l': 25.0,
                'signal': '혼전', 'signal_class': 'signal-even',
                'home_id': 'K05', 'away_id': 'K03', 'league_code': 'SC001',
                **_empty_h2h(),
                'h2h_form': '승무패승패',
                'home_pts': 41, 'away_pts': 40, 'home_rank': 7, 'away_rank': 8,
                'pts_source': 'fotmob',
            },
            {
                'date': '09/07 04:00', 'league': 'EPL', 'home': '맨체스터시티', 'away': '아스널',
                'win_allot': 2.10, 'draw_allot': 3.10, 'lose_allot': 2.80,
                'fav_team': '맨체스터시티', 'fav_side': '홈',
                'w_pct': 35.0, 'd_pct': 25.0, 'l_pct': 40.0, 'total_bet': 8500, 'match_seq': 0,
                'implied_w': 41.0, 'implied_d': 27.8, 'implied_l': 31.2,
                'signal': '역배 과몰입', 'signal_class': 'signal-upset',
                'home_id': 'E11', 'away_id': 'E01', 'league_code': 'SC101',
                'h2h_checked': True, 'h2h_team': '아스널', 'h2h_side': '원정',
                'h2h_streak': 3, 'h2h_form': '패패패승무',
                'home_pts': 38, 'away_pts': 55, 'home_rank': 10, 'away_rank': 1,
                'pts_source': 'fotmob',
            },
        ],
        'fetched_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def _normalize_publish_url(base_url):
    url = (base_url or '').strip()
    while 'https://https://' in url:
        url = url.replace('https://https://', 'https://', 1)
    while 'http://https://' in url:
        url = url.replace('http://https://', 'https://', 1)
    if url.startswith('http://http://'):
        url = 'http://' + url[len('http://http://'):]
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'https://' + url
    return url.rstrip('/')


def result_from_game_data(game_data):
    if not (game_data or {}).get('compSchedules'):
        raise RuntimeError('배당 데이터에 경기 목록이 없습니다.')
    result = parse_game_data(game_data)
    result['cached'] = False
    result['h2h_ready'] = True
    result['gm_ts'] = game_data.get('gmTs')
    _attach_points(result.get('matches') or [])
    return result


def push_payload(payload, base_url, token=''):
    if payload.get('error') and not payload.get('matches'):
        raise SystemExit(payload['error'])
    target = _normalize_publish_url(base_url) + '/api/push'
    print(f'[PUBLISH] {target}', flush=True)
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        target,
        data=body,
        headers={
            'Content-Type': 'application/json;charset=UTF-8',
            'X-Push-Token': token or '',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            print(resp.read().decode('utf-8'), flush=True)
    except urllib.error.HTTPError as exc:
        print(exc.read().decode('utf-8', errors='replace'), flush=True)
        raise SystemExit(exc.code)
    except urllib.error.URLError as exc:
        raise SystemExit(
            f'사이트 주소에 연결하지 못했습니다: {target}\n'
            f'예: python scraper.py --catch --publish https://betman-soccer.onrender.com\n'
            f'상세: {exc}'
        ) from exc


class _CatchHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS, GET')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path not in ('/', '/catch.js'):
            self.send_response(404)
            self.end_headers()
            return
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'catch.js')
        with open(path, encoding='utf-8') as fh:
            body = fh.read().encode('utf-8')
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'text/javascript; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != '/ingest':
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length)
        try:
            self.server.payload = json.loads(raw.decode('utf-8'))
            body = b'{"ok":true}'
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_response(400)
            self._cors()
            self.end_headers()


def _watch_dirs():
    home = Path.home()
    return [
        home / 'Downloads',
        home / 'Desktop',
        Path(os.path.expandvars(r'%USERPROFILE%\Downloads')),
        Path(os.path.dirname(os.path.abspath(__file__))),
        Path(tempfile.gettempdir()),
    ]


def _read_clipboard_game():
    try:
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-Command', 'Get-Clipboard -Raw'],
            capture_output=True,
            timeout=8,
        )
        text = (proc.stdout or b'').decode('utf-8', errors='replace').strip()
        if 'compSchedules' not in text:
            return None
        data = json.loads(text)
        if data.get('compSchedules'):
            return data
    except Exception:
        return None
    return None


def _load_game_file(path):
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        if data.get('compSchedules'):
            return data
    except Exception:
        return None
    return None


def _recent_game_file(since):
    seen = set()
    for folder in _watch_dirs():
        if not folder.is_dir():
            continue
        for path in list(folder.glob('betman*.json')) + list(folder.glob('*odds*.json')):
            try:
                resolved = str(path.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                if path.stat().st_mtime + 1 < since:
                    continue
            except OSError:
                continue
            data = _load_game_file(path)
            if data:
                print(f'[CATCH] 파일 {path}', flush=True)
                return data
    return None


def catch_from_browser(timeout_sec=300):
    started = time.time()
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'catch.html')
    print('', flush=True)
    print('자동 수집은 베트맨이 막습니다. 아래만 하면 됩니다.', flush=True)
    print('1) 열린 안내 페이지에서 [배당보내기]를 북마크 막대로 끌어다 놓습니다.', flush=True)
    print('2) 베트맨 프로토 승부식 화면에서 그 북마크를 누릅니다.', flush=True)
    print('3) betman-odds.json 이 받아지면 이 창이 나머지를 처리합니다.', flush=True)
    print('', flush=True)
    try:
        webbrowser.open(Path(html_path).as_uri())
    except Exception:
        pass
    try:
        webbrowser.open('https://www.betman.co.kr/main/mainPage/gamebuy/proto.do')
    except Exception:
        pass

    server = ThreadingHTTPServer(('127.0.0.1', 8765), _CatchHandler)
    server.payload = None
    server.timeout = 0.5
    deadline = time.time() + timeout_sec
    data = None
    try:
        while time.time() < deadline:
            server.handle_request()
            if server.payload and (server.payload.get('compSchedules')):
                data = server.payload
                print('[CATCH] 브라우저 전송 수신', flush=True)
                break
            data = _recent_game_file(started) or _read_clipboard_game()
            if data:
                break
            time.sleep(0.4)
    finally:
        try:
            server.server_close()
        except Exception:
            pass
    if not data:
        raise SystemExit(
            '배당 파일을 받지 못했습니다. 베트맨 승부식 화면에서 북마크 [배당보내기]를 눌러 주세요.'
        )
    print('[CATCH] 배당 수신', flush=True)
    return result_from_game_data(data)


def publish_result(base_url, token=''):
    result = _scrape_fotmob_odds(include_h2h=True)
    print(f"축구 승무패 경기: {len(result.get('matches') or [])}건", flush=True)
    push_payload(result, base_url, token)


if __name__ == '__main__':
    if '--catch' in sys.argv:
        publish_url = ''
        token = os.environ.get('PUSH_TOKEN', '')
        args = sys.argv[1:]
        if '--publish' in args:
            idx = args.index('--publish')
            if idx + 1 < len(args):
                publish_url = args[idx + 1]
            if idx + 2 < len(args) and not args[idx + 2].startswith('--'):
                token = args[idx + 2]
        result = catch_from_browser()
        print(f"축구 승무패 경기: {len(result.get('matches') or [])}건", flush=True)
        if publish_url:
            push_payload(result, publish_url, token)
        else:
            path = os.path.join(tempfile.gettempdir(), 'betman-publish.json')
            _atomic_write_json(path, result)
            print('[CATCH] 저장', path, flush=True)
        raise SystemExit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == '--dump':
        dump_result(sys.argv[2])
        raise SystemExit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == '--publish':
        token = sys.argv[3] if len(sys.argv) > 3 else os.environ.get('PUSH_TOKEN', '')
        publish_result(sys.argv[2], token)
        raise SystemExit(0)
    result = fetch_betman_data(force=True)
    print(f"회차: {result['round_info']}")
    print(f"마감: {result['sale_end']}")
    print(f"수집: {result['fetched_at']}")
    print(f"축구 승무패 경기: {len(result['matches'])}건")
    print()
    for m in result['matches'][:12]:
        print(
            f"  [{m['league']}] {m['date']} | {m['home']} vs {m['away']} | "
            f"승:{m['win_allot']} 무:{m['draw_allot']} 패:{m['lose_allot']} | "
            f"정배당: {m['fav_team']}({m['fav_side']}) | {m['signal']}"
            + (
                f" | ✓ {m['h2h_team']} {m['h2h_streak']}연승"
                if m.get('h2h_checked') else
                f" | 상대전적 {m.get('h2h_form') or '-'}"
            )
            + (
                f" | 승점 {m.get('home_pts')}-{m.get('away_pts')}"
                if m.get('home_pts') is not None and m.get('away_pts') is not None
                else ''
            )
        )
