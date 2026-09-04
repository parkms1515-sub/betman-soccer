"""
Betman Soccer Rule Analyzer - 베트맨 축구 배당률 분석기
Flask 웹 애플리케이션
"""
import os

from flask import Flask, render_template_string, jsonify, request
from scraper import fetch_betman_data

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>베트맨 축구 배당률 분석기</title>
    <meta name="description" content="베트맨 프로토 승부식 축구 배당률 실시간 분석">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(135deg, #0f0c29 0%, #1a1a3e 50%, #24243e 100%);
            color: #e0e0e0;
            min-height: 100vh;
        }

        .container {
            max-width: 1540px;
            margin: 0 auto;
            padding: 20px;
        }

        header {
            text-align: center;
            padding: 30px 0 20px;
        }

        header h1 {
            font-size: 2rem;
            font-weight: 700;
            background: linear-gradient(135deg, #00d2ff, #7b2ff7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin-bottom: 8px;
        }

        .meta-info {
            display: flex;
            justify-content: center;
            gap: 24px;
            font-size: 0.85rem;
            color: #8888aa;
            flex-wrap: wrap;
        }

        .meta-info span {
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .meta-info .label { color: #6c6c8a; }
        .meta-info .value { color: #00d2ff; font-weight: 600; }

        .stats-bar {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            margin: 20px 0;
        }

        .stat-card {
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 12px;
            padding: 16px;
            text-align: center;
            backdrop-filter: blur(10px);
            transition: transform 0.2s, border-color 0.2s;
        }

        .stat-card:hover {
            transform: translateY(-2px);
            border-color: rgba(0, 210, 255, 0.3);
        }

        .stat-card .number {
            font-size: 1.8rem;
            font-weight: 700;
            background: linear-gradient(135deg, #00d2ff, #7b2ff7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        .stat-card .desc {
            font-size: 0.8rem;
            color: #6c6c8a;
            margin-top: 4px;
        }

        .filter-bar {
            display: flex;
            gap: 8px;
            margin: 20px 0 16px;
            flex-wrap: wrap;
            align-items: center;
        }

        .filter-btn {
            padding: 8px 16px;
            border: 1px solid rgba(255, 255, 255, 0.12);
            background: rgba(255, 255, 255, 0.04);
            color: #aaa;
            border-radius: 20px;
            cursor: pointer;
            font-size: 0.82rem;
            transition: all 0.2s;
            font-family: inherit;
        }

        .filter-btn:hover, .filter-btn.active {
            background: rgba(0, 210, 255, 0.15);
            border-color: rgba(0, 210, 255, 0.4);
            color: #00d2ff;
        }

        .refresh-btn {
            margin-left: auto;
            background: rgba(123, 47, 247, 0.18);
            border-color: rgba(123, 47, 247, 0.4);
            color: #c4a0ff;
        }

        .table-wrapper {
            overflow-x: auto;
            border-radius: 16px;
            border: 1px solid rgba(255, 255, 255, 0.06);
            background: rgba(255, 255, 255, 0.02);
            backdrop-filter: blur(10px);
            min-height: 220px;
            position: relative;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.88rem;
        }

        thead th {
            background: rgba(255, 255, 255, 0.05);
            padding: 14px 12px;
            text-align: center;
            font-weight: 600;
            font-size: 0.78rem;
            color: #8888aa;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            position: sticky;
            top: 0;
            white-space: nowrap;
        }

        tbody tr {
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            transition: background 0.15s;
        }

        tbody tr:hover { background: rgba(0, 210, 255, 0.04); }
        tbody tr.h2h-hot { background: rgba(255, 171, 64, 0.05); }

        td {
            padding: 12px;
            text-align: center;
            white-space: nowrap;
        }

        .league-badge {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 0.72rem;
            font-weight: 600;
        }

        .league-k1 { background: rgba(0, 200, 83, 0.15); color: #00c853; }
        .league-k2 { background: rgba(255, 152, 0, 0.15); color: #ff9800; }
        .league-epl { background: rgba(156, 39, 176, 0.15); color: #ce93d8; }
        .league-default { background: rgba(100, 100, 140, 0.15); color: #8888bb; }

        .team-name { font-weight: 500; color: #ddd; }
        .odds { font-weight: 600; font-variant-numeric: tabular-nums; }
        .odds-win { color: #4fc3f7; }
        .odds-draw { color: #aaa; }
        .odds-lose { color: #ef9a9a; }
        .odds-lowest {
            background: rgba(0, 210, 255, 0.1);
            border-radius: 6px;
            padding: 4px 8px;
        }

        .fav-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 8px;
            font-size: 0.78rem;
            font-weight: 600;
        }

        .fav-home {
            background: rgba(0, 210, 255, 0.12);
            color: #4fc3f7;
            border: 1px solid rgba(0, 210, 255, 0.2);
        }

        .fav-away {
            background: rgba(239, 154, 154, 0.12);
            color: #ef9a9a;
            border: 1px solid rgba(239, 154, 154, 0.2);
        }

        .fav-even {
            background: rgba(255, 255, 255, 0.06);
            color: #bbb;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }

        .signal-badge {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 8px;
            font-size: 0.74rem;
            font-weight: 600;
        }

        .signal-upset { background: rgba(255, 82, 82, 0.16); color: #ff8a80; }
        .signal-crowd { background: rgba(255, 193, 7, 0.14); color: #ffd54f; }
        .signal-fav { background: rgba(0, 210, 255, 0.12); color: #4fc3f7; }
        .signal-even { background: rgba(255, 255, 255, 0.05); color: #999; }

        .h2h-streak {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 8px;
            font-size: 0.74rem;
            font-weight: 700;
        }

        .h2h-home {
            background: rgba(0, 210, 255, 0.14);
            color: #4fc3f7;
            border: 1px solid rgba(0, 210, 255, 0.25);
        }

        .h2h-away {
            background: rgba(255, 171, 64, 0.16);
            color: #ffb74d;
            border: 1px solid rgba(255, 171, 64, 0.28);
        }

        .h2h-none { color: #666; font-size: 0.78rem; }
        .h2h-form { font-size: 0.68rem; color: #777; margin-top: 3px; letter-spacing: 1px; }

        .vote-bar {
            display: flex;
            height: 6px;
            border-radius: 3px;
            overflow: hidden;
            min-width: 80px;
            margin-top: 4px;
        }

        .vote-w { background: #4fc3f7; }
        .vote-d { background: #666; }
        .vote-l { background: #ef9a9a; }
        .vote-text { font-size: 0.72rem; color: #777; }

        .no-data {
            text-align: center;
            padding: 60px 20px;
            color: #666;
        }

        .no-data .icon { font-size: 3rem; margin-bottom: 12px; }

        .refresh-notice {
            text-align: center;
            margin-top: 16px;
            font-size: 0.78rem;
            color: #555;
        }

        .loading {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 14px;
            padding: 70px 20px;
            color: #8888aa;
        }

        .spinner {
            width: 36px;
            height: 36px;
            border: 3px solid rgba(255, 255, 255, 0.12);
            border-top-color: #00d2ff;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }

        @keyframes spin { to { transform: rotate(360deg); } }

        @media (max-width: 768px) {
            header h1 { font-size: 1.4rem; }
            .container { padding: 12px; }
            table { font-size: 0.8rem; }
            td, thead th { padding: 8px 6px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>⚽ 베트맨 축구 배당률 분석기</h1>
            <div class="meta-info">
                <span><span class="label">프로토 회차</span> <span class="value" id="roundInfo">-</span></span>
                <span><span class="label">발매 마감</span> <span class="value" id="saleEnd">-</span></span>
                <span><span class="label">데이터 수집</span> <span class="value" id="fetchedAt">불러오는 중</span></span>
            </div>
        </header>

        <div class="stats-bar">
            <div class="stat-card">
                <div class="number" id="matchCount">-</div>
                <div class="desc">축구 승무패 경기</div>
            </div>
            <div class="stat-card">
                <div class="number" id="leagueCount">-</div>
                <div class="desc">참여 리그</div>
            </div>
            <div class="stat-card">
                <div class="number" id="homeFavCount">-</div>
                <div class="desc">홈 정배당</div>
            </div>
            <div class="stat-card">
                <div class="number" id="awayFavCount">-</div>
                <div class="desc">원정 정배당</div>
            </div>
            <div class="stat-card">
                <div class="number" id="h2hStreakCount">-</div>
                <div class="desc">상대전적 3연승+</div>
            </div>
        </div>

        <div class="filter-bar" id="filterBar">
            <button class="filter-btn active" data-league="all">전체</button>
            <button class="filter-btn refresh-btn" id="refreshBtn">새로고침</button>
        </div>

        <div class="table-wrapper">
            <table id="matchTable">
                <thead>
                    <tr>
                        <th>일시</th>
                        <th>리그</th>
                        <th>홈</th>
                        <th>승 배당</th>
                        <th>무 배당</th>
                        <th>패 배당</th>
                        <th>원정</th>
                        <th>정배당</th>
                        <th>투표 현황</th>
                        <th>분석</th>
                        <th>상대전적</th>
                    </tr>
                </thead>
                <tbody id="matchBody">
                    <tr>
                        <td colspan="11">
                            <div class="loading">
                                <div class="spinner"></div>
                                <div>베트맨에서 배당과 상대전적을 가져오는 중입니다. 첫 수집은 조금 더 걸릴 수 있습니다.</div>
                            </div>
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>

        <div class="refresh-notice">
            데이터는 3분간 캐시됩니다. 새로고침을 누르면 베트맨에서 다시 수집합니다.
        </div>
    </div>

    <script>
        let allMatches = [];
        let currentLeague = 'all';

        function leagueClass(league) {
            if (league.includes('K리1') || league.includes('K리그1')) return 'league-k1';
            if (league.includes('K리2') || league.includes('K리그2')) return 'league-k2';
            if (league.includes('EPL') || league === 'PL') return 'league-epl';
            return 'league-default';
        }

        let h2hReady = false;
        let h2hPollTimer = null;

        function h2hCell(m) {
            const form = m.h2h_form
                ? `<div class="h2h-form">${escapeHtml(m.h2h_form)}</div>`
                : '';
            if (m.h2h_checked && m.h2h_streak >= 3) {
                const cls = m.h2h_side === '홈' ? 'h2h-home' : 'h2h-away';
                return `<span class="h2h-streak ${cls}">✓ ${escapeHtml(m.h2h_team)} ${m.h2h_streak}연승</span>${form}`;
            }
            if (m.h2h_form) {
                return `<span class="h2h-none">${escapeHtml(m.h2h_form)}</span>`;
            }
            if (!h2hReady) {
                return '<span class="h2h-none">조회중</span>';
            }
            return '<span class="h2h-none">-</span>';
        }

        function favClass(side) {
            if (side === '홈') return 'fav-home';
            if (side === '원정') return 'fav-away';
            return 'fav-even';
        }

        function escapeHtml(text) {
            return String(text ?? '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;');
        }

        function renderFilters(leagues) {
            const bar = document.getElementById('filterBar');
            const refresh = document.getElementById('refreshBtn');
            bar.querySelectorAll('.filter-btn:not(.refresh-btn)').forEach(btn => btn.remove());
            const allBtn = document.createElement('button');
            allBtn.className = 'filter-btn' + (currentLeague === 'all' ? ' active' : '');
            allBtn.dataset.league = 'all';
            allBtn.textContent = '전체';
            bar.insertBefore(allBtn, refresh);
            const streakBtn = document.createElement('button');
            streakBtn.className = 'filter-btn' + (currentLeague === 'streak' ? ' active' : '');
            streakBtn.dataset.league = 'streak';
            streakBtn.textContent = '3연승+';
            bar.insertBefore(streakBtn, refresh);
            leagues.forEach(league => {
                const btn = document.createElement('button');
                btn.className = 'filter-btn' + (currentLeague === league ? ' active' : '');
                btn.dataset.league = league;
                btn.textContent = league;
                bar.insertBefore(btn, refresh);
            });
        }

        function renderRows() {
            const body = document.getElementById('matchBody');
            const rows = allMatches.filter(m => {
                if (currentLeague === 'streak') return !!m.h2h_checked;
                return currentLeague === 'all' || m.league === currentLeague;
            });
            if (!rows.length) {
                body.innerHTML = `
                    <tr><td colspan="11">
                        <div class="no-data">
                            <div class="icon">📊</div>
                            <div>표시할 축구 승무패 경기가 없습니다.</div>
                        </div>
                    </td></tr>`;
                return;
            }
            body.innerHTML = rows.map(m => `
                <tr data-league="${escapeHtml(m.league)}" class="${m.h2h_checked ? 'h2h-hot' : ''}">
                    <td style="color:#888;">${escapeHtml(m.date)}</td>
                    <td><span class="league-badge ${leagueClass(m.league)}">${escapeHtml(m.league)}</span></td>
                    <td class="team-name">${escapeHtml(m.home)}</td>
                    <td class="odds odds-win ${m.fav_side === '홈' ? 'odds-lowest' : ''}">${m.win_allot}</td>
                    <td class="odds odds-draw">${m.draw_allot}</td>
                    <td class="odds odds-lose ${m.fav_side === '원정' ? 'odds-lowest' : ''}">${m.lose_allot}</td>
                    <td class="team-name">${escapeHtml(m.away)}</td>
                    <td><span class="fav-badge ${favClass(m.fav_side)}">${escapeHtml(m.fav_team)}</span></td>
                    <td>
                        <div class="vote-text">승${m.w_pct}% 무${m.d_pct}% 패${m.l_pct}%</div>
                        <div class="vote-bar">
                            <div class="vote-w" style="width:${m.w_pct}%"></div>
                            <div class="vote-d" style="width:${m.d_pct}%"></div>
                            <div class="vote-l" style="width:${m.l_pct}%"></div>
                        </div>
                    </td>
                    <td><span class="signal-badge ${escapeHtml(m.signal_class)}">${escapeHtml(m.signal)}</span></td>
                    <td>${h2hCell(m)}</td>
                </tr>
            `).join('');
        }

        function applyData(data) {
            allMatches = data.matches || [];
            document.getElementById('roundInfo').textContent = (data.round_info || '-') + '회';
            document.getElementById('saleEnd').textContent = data.sale_end || '-';
            const cacheTag = data.cached ? ' (캐시)' : '';
            document.getElementById('fetchedAt').textContent = (data.fetched_at || '-') + cacheTag;
            document.getElementById('matchCount').textContent = allMatches.length;
            document.getElementById('leagueCount').textContent = (data.leagues || []).length;
            document.getElementById('homeFavCount').textContent = data.home_fav_count ?? 0;
            document.getElementById('awayFavCount').textContent = data.away_fav_count ?? 0;
            document.getElementById('h2hStreakCount').textContent = data.h2h_streak_count ?? 0;
            h2hReady = !!data.h2h_ready;
            renderFilters(data.leagues || []);
            renderRows();
            if (!h2hReady) scheduleH2hPoll();
        }

        function scheduleH2hPoll() {
            if (h2hPollTimer) return;
            let tries = 0;
            h2hPollTimer = setInterval(async () => {
                tries += 1;
                if (tries > 20) {
                    clearInterval(h2hPollTimer);
                    h2hPollTimer = null;
                    h2hReady = true;
                    renderRows();
                    return;
                }
                try {
                    const res = await fetch('/api/data');
                    if (!res.ok) return;
                    const data = await res.json();
                    applyData(data);
                    if (data.h2h_ready) {
                        clearInterval(h2hPollTimer);
                        h2hPollTimer = null;
                    }
                } catch (err) {
                    /* keep polling */
                }
            }, 3000);
        }

        async function loadData(force) {
            if (h2hPollTimer) {
                clearInterval(h2hPollTimer);
                h2hPollTimer = null;
            }
            const body = document.getElementById('matchBody');
            body.innerHTML = `
                <tr><td colspan="11">
                    <div class="loading">
                        <div class="spinner"></div>
                        <div>${force ? '최신 배당을 다시 수집하는 중...' : '베트맨에서 최신 배당을 가져오는 중입니다. 약 15초 걸릴 수 있습니다.'}</div>
                    </div>
                </td></tr>`;
            try {
                const url = force ? '/api/data?refresh=1' : '/api/data';
                const res = await fetch(url);
                if (!res.ok) throw new Error('HTTP ' + res.status);
                applyData(await res.json());
            } catch (err) {
                body.innerHTML = `
                    <tr><td colspan="11">
                        <div class="no-data">
                            <div class="icon">⚠️</div>
                            <div>데이터를 불러오지 못했습니다. 새로고침을 다시 눌러 주세요.</div>
                        </div>
                    </td></tr>`;
            }
        }

        document.getElementById('filterBar').addEventListener('click', (event) => {
            const btn = event.target.closest('.filter-btn');
            if (!btn) return;
            if (btn.id === 'refreshBtn') {
                loadData(true);
                return;
            }
            currentLeague = btn.dataset.league;
            document.querySelectorAll('.filter-btn:not(.refresh-btn)').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            renderRows();
        });

        loadData(false);
    </script>
</body>
</html>
"""


def _payload(force=False):
    data = fetch_betman_data(force=force)
    matches = data.get('matches', [])
    leagues = list(dict.fromkeys(m['league'] for m in matches))
    return {
        'matches': matches,
        'leagues': leagues,
        'round_info': data.get('round_info', '-'),
        'sale_end': data.get('sale_end', '-'),
        'fetched_at': data.get('fetched_at', '-'),
        'cached': data.get('cached', False),
        'home_fav_count': sum(1 for m in matches if m.get('fav_side') == '홈'),
        'away_fav_count': sum(1 for m in matches if m.get('fav_side') == '원정'),
        'h2h_streak_count': sum(1 for m in matches if m.get('h2h_checked')),
        'h2h_ready': bool(data.get('h2h_ready')),
    }


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/data')
def api_data():
    force = request.args.get('refresh') == '1'
    return jsonify(_payload(force=force))


@app.route('/health')
def health():
    return jsonify({'ok': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG') == '1')
