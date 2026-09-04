"""
베트맨(Betman.co.kr) 프로토 승부식 실시간 배당률 스크래퍼
Playwright로 실제 브라우저 접속 후 gameInfoInq.do API 응답을 가로채 추출합니다.
"""
from playwright.sync_api import sync_playwright
from datetime import datetime
import json
import os
import threading
import time

CACHE_TTL_SEC = 180
H2H_CACHE_TTL_SEC = 3600
_CACHE = {'data': None, 'ts': 0.0}
_H2H_CACHE = {}
_SCRAPE_LOCK = threading.Lock()
_H2H_GEN = 0


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
        args.extend(['--single-process', '--no-zygote'])
    return args


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
        return _scrape_odds()


def _scrape_odds():
    game_data = {}
    buy_data = {}

    def handle_response(response):
        url = response.url
        try:
            if 'inqCacheBuyAbleGameInfoList.do' in url:
                buy_data.update(response.json())
            elif 'gameInfoInq.do' in url and 'Asis' not in url:
                game_data.update(response.json())
        except Exception:
            pass

    p = sync_playwright().start()
    browser = None
    result = None
    try:
        browser = p.chromium.launch(headless=True, args=_chromium_args())
        page = browser.new_page()
        page.on('response', handle_response)

        page.goto(
            'https://www.betman.co.kr/main/mainPage/gamebuy/proto.do',
            timeout=30000,
        )
        page.wait_for_timeout(2500)

        gm_ts = None
        for game in buy_data.get('protoGames', []):
            if game.get('gmId') == 'G101':
                gm_ts = game.get('gmTs')
                break
        if not gm_ts:
            gm_ts = 260105

        try:
            with page.expect_response(
                lambda r: 'gameInfoInq.do' in r.url and 'Asis' not in r.url,
                timeout=25000,
            ):
                page.goto(
                    f'https://www.betman.co.kr/main/mainPage/gamebuy/gameSlip.do?gmId=G101&gmTs={gm_ts}',
                    timeout=30000,
                )
            page.wait_for_timeout(500)
        except Exception:
            page.wait_for_timeout(6000)

        if not game_data.get('compSchedules'):
            print('[ERROR] 베트맨 API 응답을 받지 못했습니다.')
            result = get_dummy_result()
        else:
            result = parse_game_data(game_data)
            result['cached'] = False
            result['h2h_ready'] = False
            result['gm_ts'] = game_data.get('gmTs') or gm_ts
            _CACHE['data'] = result
            _CACHE['ts'] = time.time()
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        p.stop()

    if result and not result.get('h2h_ready'):
        _start_h2h_thread(result)
    return result or get_dummy_result()


def _start_h2h_thread(result):
    global _H2H_GEN
    _H2H_GEN += 1
    gen = _H2H_GEN
    thread = threading.Thread(
        target=_h2h_worker,
        args=(gen, result),
        daemon=True,
    )
    thread.start()


def _h2h_worker(gen, result):
    """배당 응답 후에 상대전적만 따로 채웁니다. 헬스체크를 막지 않습니다."""
    gm_ts = result.get('gm_ts') or 260105
    matches = result.get('matches') or []
    p = sync_playwright().start()
    browser = None
    try:
        browser = p.chromium.launch(headless=True, args=_chromium_args())
        page = browser.new_page()
        page.goto(
            f'https://www.betman.co.kr/main/mainPage/gamebuy/gameSlip.do?gmId=G101&gmTs={gm_ts}',
            timeout=30000,
        )
        page.wait_for_timeout(1500)
        if gen != _H2H_GEN:
            return
        _attach_h2h_streaks(page, matches, gm_ts)
        if gen == _H2H_GEN:
            result['h2h_ready'] = True
    except Exception as exc:
        print('[WARN] 상대전적 백그라운드 수집 실패:', exc)
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        p.stop()


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
            },
        ],
        'fetched_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


if __name__ == '__main__':
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
        )
