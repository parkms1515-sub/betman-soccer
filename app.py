"""
프로토 축구 배당 분석기 - FotMob 참고 배당 Flask 앱
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

from flask import Flask, render_template_string, jsonify, request

app = Flask(__name__)

DUMP_PATH = os.path.join(tempfile.gettempdir(), 'betman.json')
CACHE_TTL_SEC = 180
_STATE = {
    'data': None,
    'ts': 0.0,
    'running': False,
    'error': None,
}
_STATE_LOCK = threading.Lock()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>프로토 축구 배당 분석기</title>
    <meta name="description" content="프로토 축구 승무패 참고 배당(FotMob) 분석">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        html { -webkit-text-size-adjust: 100%; }
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

        .filter-wrap { margin: 20px 0 16px; }
        .filter-bar {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            align-items: center;
        }
        .filter-bar.league-bar { margin-top: 8px; }

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
            touch-action: manipulation;
            -webkit-tap-highlight-color: transparent;
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
        tbody tr.pts-hot { background: rgba(255, 82, 82, 0.06); }
        tbody tr.form5-hot { background: rgba(129, 199, 132, 0.06); }

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
        .form5-lines { display: flex; flex-direction: column; gap: 6px; }
        .form5-line { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
        .form5-marks { letter-spacing: 2px; font-weight: 700; font-size: 0.82rem; color: #ddd; }

        .pts-hot-badge {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 8px;
            font-size: 0.74rem;
            font-weight: 700;
            background: rgba(255, 82, 82, 0.16);
            color: #ff8a80;
            border: 1px solid rgba(255, 82, 82, 0.28);
        }

        .pts-plus-badge, .pts-minus-badge {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 8px;
            font-size: 0.74rem;
            font-weight: 700;
        }
        .pts-plus-badge {
            background: rgba(0, 210, 255, 0.16);
            color: #4fc3f7;
            border: 1px solid rgba(0, 210, 255, 0.28);
        }
        .pts-minus-badge {
            background: rgba(255, 171, 64, 0.16);
            color: #ffb74d;
            border: 1px solid rgba(255, 171, 64, 0.28);
        }

        .pts-norm-badge {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 8px;
            font-size: 0.74rem;
            font-weight: 600;
            background: rgba(255, 255, 255, 0.05);
            color: #bbb;
        }

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

        .card-list { display: none; }
        .match-card {
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 14px;
            padding: 12px;
        }
        .match-card.h2h-hot { border-color: rgba(255, 171, 64, 0.35); }
        .match-card.pts-hot { border-color: rgba(255, 82, 82, 0.4); }
        .match-card.form5-hot { border-color: rgba(129, 199, 132, 0.4); }
        .card-top {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 10px;
            font-size: 0.75rem;
            color: #8888aa;
        }
        .card-top .date { flex: 1; }
        .card-teams {
            display: grid;
            grid-template-columns: 1fr auto 1fr;
            gap: 8px;
            align-items: center;
            text-align: center;
        }
        .card-teams .name {
            font-weight: 600;
            color: #eee;
            white-space: normal;
            word-break: keep-all;
            line-height: 1.3;
            font-size: 0.92rem;
        }
        .card-teams .odds { font-size: 1.15rem; margin-top: 4px; }
        .card-draw { color: #888; font-size: 0.72rem; }
        .card-meta {
            margin-top: 10px;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
            justify-content: space-between;
        }
        .card-meta .vote-text { white-space: normal; }

        @media (max-width: 768px) {
            header h1 { font-size: 1.25rem; }
            .container { padding: 12px 12px calc(16px + env(safe-area-inset-bottom)); }
            .meta-info { gap: 12px; font-size: 0.78rem; }
            .stats-bar { grid-template-columns: repeat(3, 1fr); gap: 8px; }
            .stat-card { padding: 10px 6px; }
            .stat-card .number { font-size: 1.15rem; }
            .stat-card .desc { font-size: 0.68rem; }
            .filter-bar.league-bar {
                flex-wrap: nowrap;
                overflow-x: auto;
                -webkit-overflow-scrolling: touch;
                padding-bottom: 6px;
            }
            .filter-btn { flex: 0 0 auto; min-height: 36px; }
            .refresh-btn { margin-left: auto; }
            .table-wrapper { display: none; }
            .card-list { display: flex; flex-direction: column; gap: 10px; }
            .refresh-notice { font-size: 0.75rem; line-height: 1.5; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>⚽ 프로토 축구 배당 분석기</h1>
            <div class="meta-info">
                <span><span class="label">일정</span> <span class="value" id="roundInfo">-</span></span>
                <span><span class="label">마지막 경기</span> <span class="value" id="saleEnd">-</span></span>
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
            <div class="stat-card">
                <div class="number" id="ptsOutlierCount">-</div>
                <div class="desc">승점차이 이탈</div>
            </div>
            <div class="stat-card">
                <div class="number" id="form5Count">-</div>
                <div class="desc">최근5 9점+</div>
            </div>
        </div>

        <div class="filter-wrap" id="filterWrap">
            <div class="filter-bar" id="filterBar">
                <button class="filter-btn active" data-league="all">전체</button>
                <button class="filter-btn" data-league="streak">3연승+</button>
                <button class="filter-btn" data-league="pts">승점차이</button>
                <button class="filter-btn" data-league="form5">승점9점</button>
                <button class="filter-btn refresh-btn" id="refreshBtn">새로고침</button>
            </div>
            <div class="filter-bar league-bar" id="leagueBar"></div>
        </div>

        <div class="table-wrapper">
            <table id="matchTable">
                <thead>
                    <tr id="matchHead"></tr>
                </thead>
                <tbody id="matchBody">
                    <tr>
                        <td colspan="13">
                            <div class="loading">
                                <div class="spinner"></div>
                                <div>FotMob에서 축구 배당을 가져오는 중입니다. 첫 수집은 조금 더 걸릴 수 있습니다.</div>
                            </div>
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>

        <div class="card-list" id="matchCards"></div>

        <div class="refresh-notice">
            배당·승점·순위는 FotMob 참고 값입니다. 승점차이는 이번에 올라온 경기에서 같은 승점차의 평균 배당과 비교해, 편차가 ±로 큰 경기만 보여 줍니다. 승점9점은 리그 최근 5경기 승점이 9점 이상인 팀입니다. 컵·친선은 넣지 않습니다.
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

        function toNum(value) {
            if (value === null || value === undefined || value === '') return null;
            const num = Number(value);
            return Number.isFinite(num) ? num : null;
        }

        function linReg(xs, ys) {
            const n = xs.length;
            const mx = xs.reduce((a, b) => a + b, 0) / n;
            const my = ys.reduce((a, b) => a + b, 0) / n;
            let den = 0;
            let num = 0;
            for (let i = 0; i < n; i++) {
                den += (xs[i] - mx) ** 2;
                num += (xs[i] - mx) * (ys[i] - my);
            }
            const slope = den < 1e-9 ? 0 : num / den;
            return { slope, intercept: my - slope * mx };
        }

        function stdev(values) {
            if (values.length < 2) return 0;
            const mean = values.reduce((a, b) => a + b, 0) / values.length;
            const variance = values.reduce((a, b) => a + (b - mean) ** 2, 0) / (values.length - 1);
            return Math.sqrt(variance);
        }

        function mean(values) {
            if (!values.length) return 0;
            return values.reduce((a, b) => a + b, 0) / values.length;
        }

        function annotatePts(matches) {
            const samples = [];
            matches.forEach(m => {
                const homePts = toNum(m.home_pts);
                const awayPts = toNum(m.away_pts);
                const win = toNum(m.win_allot);
                const lose = toNum(m.lose_allot);
                m.pts_diff = (homePts !== null && awayPts !== null) ? homePts - awayPts : null;
                m.odds_gap = (win !== null && lose !== null) ? +(lose - win).toFixed(2) : null;
                m.pts_expected_gap = null;
                m.pts_residual = null;
                m.pts_outlier = false;
                m.pts_signal = '';
                if (m.pts_diff !== null && m.odds_gap !== null) samples.push(m);
            });
            if (samples.length < 5) return;

            const xs = samples.map(m => m.pts_diff);
            const ys = samples.map(m => m.odds_gap);
            const fit = linReg(xs, ys);

            samples.forEach(m => {
                const near = samples.filter(other => (
                    other !== m && Math.abs(other.pts_diff - m.pts_diff) <= 3
                ));
                const expected = near.length >= 3
                    ? mean(near.map(other => other.odds_gap))
                    : (fit.slope * m.pts_diff + fit.intercept);
                m.pts_expected_gap = +expected.toFixed(2);
                m.pts_residual = +(m.odds_gap - expected).toFixed(2);
            });

            const residuals = samples.map(m => m.pts_residual);
            const sigma = stdev(residuals) || 0.4;
            const threshold = Math.max(0.45, sigma);
            samples.forEach(m => {
                m.pts_outlier = Math.abs(m.pts_residual) >= threshold;
                if (!m.pts_outlier) {
                    m.pts_signal = '';
                } else if (m.pts_residual > 0) {
                    m.pts_signal = '+편차 홈우세 과대';
                } else {
                    m.pts_signal = '-편차 원정우세 과대';
                }
            });
        }

        function ptsCell(m) {
            if (m.pts_diff === null || m.pts_diff === undefined) {
                return '<span class="h2h-none">-</span>';
            }
            const diffText = (m.pts_diff > 0 ? '+' : '') + m.pts_diff + '점';
            let cls = 'pts-norm-badge';
            if (m.pts_outlier && m.pts_residual > 0) cls = 'pts-plus-badge';
            else if (m.pts_outlier && m.pts_residual < 0) cls = 'pts-minus-badge';
            else if (m.pts_outlier) cls = 'pts-hot-badge';
            const avg = m.pts_expected_gap == null
                ? ''
                : `평균 ${(m.pts_expected_gap > 0 ? '+' : '') + m.pts_expected_gap}`;
            const resid = m.pts_residual == null
                ? ''
                : `편차 ${(m.pts_residual > 0 ? '+' : '') + m.pts_residual}`;
            const detail = [avg, resid].filter(Boolean).join(' · ');
            const signal = (m.pts_outlier && m.pts_signal)
                ? `<div class="h2h-form">${escapeHtml(m.pts_signal)}</div>`
                : '';
            return `<span class="${cls}">${diffText}</span>` +
                `<div class="h2h-form">${m.home_pts}-${m.away_pts}${detail ? ' · ' + detail : ''}</div>` +
                signal;
        }

        function form5Cell(m) {
            const lines = [];
            const add = (side, pts, form, hotClass) => {
                if (pts == null && !form) return;
                const hot = Number(pts) >= 9;
                const label = hot
                    ? `<span class="h2h-streak ${hotClass}">✓ ${side} ${pts}점</span>`
                    : (pts != null ? `<span class="h2h-none">${side} ${pts}점</span>` : '');
                const marks = form
                    ? `<span class="form5-marks">${escapeHtml(form)}</span>`
                    : '';
                lines.push(`<div class="form5-line">${label}${marks}</div>`);
            };
            add('홈', m.form5_home_pts, m.form5_home, 'h2h-home');
            add('원정', m.form5_away_pts, m.form5_away, 'h2h-away');
            return lines.length ? `<div class="form5-lines">${lines.join('')}</div>` : '<span class="h2h-none">-</span>';
        }

        function isForm5View() {
            return currentLeague === 'form5';
        }

        function tableColspan() {
            return isForm5View() ? 10 : 13;
        }

        function renderHead() {
            const extra = isForm5View()
                ? ''
                : '<th>기대확률</th><th>분석</th><th>승점차</th>';
            document.getElementById('matchHead').innerHTML = `
                <th>일시</th>
                <th>리그</th>
                <th>홈</th>
                <th>승 배당</th>
                <th>무 배당</th>
                <th>패 배당</th>
                <th>원정</th>
                <th>정배당</th>
                ${extra}
                <th>최근5</th>
                <th>상대전적</th>`;
        }

        function rowHotClass(m) {
            if (m.pts_outlier) return 'pts-hot';
            if (m.form5_checked) return 'form5-hot';
            if (m.h2h_checked) return 'h2h-hot';
            return '';
        }

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
            document.querySelectorAll('#filterBar .filter-btn:not(.refresh-btn)').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.league === currentLeague);
            });
            const bar = document.getElementById('leagueBar');
            bar.innerHTML = '';
            (leagues || []).forEach(league => {
                const btn = document.createElement('button');
                btn.className = 'filter-btn' + (currentLeague === league ? ' active' : '');
                btn.dataset.league = league;
                btn.textContent = league;
                bar.appendChild(btn);
            });
        }

        function matchCard(m) {
            const hot = rowHotClass(m);
            const slim = isForm5View();
            const signal = slim ? '' : `<span class="signal-badge ${escapeHtml(m.signal_class)}">${escapeHtml(m.signal)}</span>`;
            const extraMeta = slim ? form5Cell(m) : `${ptsCell(m)}${form5Cell(m)}${h2hCell(m)}`;
            const vote = slim ? '' : `
                    <div class="vote-text">승${m.w_pct}% 무${m.d_pct}% 패${m.l_pct}%</div>
                    <div class="vote-bar">
                        <div class="vote-w" style="width:${m.w_pct}%"></div>
                        <div class="vote-d" style="width:${m.d_pct}%"></div>
                        <div class="vote-l" style="width:${m.l_pct}%"></div>
                    </div>`;
            return `
                <article class="match-card ${hot}">
                    <div class="card-top">
                        <span class="date">${escapeHtml(m.date)}</span>
                        <span class="league-badge ${leagueClass(m.league)}">${escapeHtml(m.league)}</span>
                        ${signal}
                    </div>
                    <div class="card-teams">
                        <div>
                            <div class="name">${escapeHtml(m.home)}</div>
                            <div class="odds odds-win ${m.fav_side === '홈' ? 'odds-lowest' : ''}">${m.win_allot}</div>
                        </div>
                        <div>
                            <div class="card-draw">무</div>
                            <div class="odds odds-draw">${m.draw_allot}</div>
                        </div>
                        <div>
                            <div class="name">${escapeHtml(m.away)}</div>
                            <div class="odds odds-lose ${m.fav_side === '원정' ? 'odds-lowest' : ''}">${m.lose_allot}</div>
                        </div>
                    </div>
                    <div class="card-meta">
                        <span class="fav-badge ${favClass(m.fav_side)}">${escapeHtml(m.fav_team)}</span>
                        ${extraMeta}
                        ${slim ? h2hCell(m) : ''}
                    </div>
                    ${vote}
                </article>`;
        }

        function setStatus(html) {
            renderHead();
            document.getElementById('matchBody').innerHTML =
                `<tr><td colspan="${tableColspan()}">${html}</td></tr>`;
            document.getElementById('matchCards').innerHTML = html;
        }

        function kickoffMs(m) {
            if (m && m.kickoff) {
                const parsed = Date.parse(m.kickoff);
                if (!Number.isNaN(parsed)) return parsed;
            }
            const text = String((m && m.date) || '');
            const hit = text.match(/^(\\d{1,2})\\/(\\d{1,2})\\s+(\\d{1,2}):(\\d{2})$/);
            if (!hit) return Number.POSITIVE_INFINITY;
            const now = new Date();
            const dt = new Date(now.getFullYear(), Number(hit[1]) - 1, Number(hit[2]), Number(hit[3]), Number(hit[4]));
            const diffDays = (now - dt) / 86400000;
            if (diffDays > 180) dt.setFullYear(now.getFullYear() + 1);
            else if (diffDays < -180) dt.setFullYear(now.getFullYear() - 1);
            return dt.getTime();
        }

        function sortByKickoff(rows) {
            return rows.slice().sort((a, b) => {
                const diff = kickoffMs(a) - kickoffMs(b);
                if (diff) return diff;
                return String(a.home || '').localeCompare(String(b.home || ''), 'ko');
            });
        }

        function renderRows() {
            const body = document.getElementById('matchBody');
            const cards = document.getElementById('matchCards');
            let rows = allMatches.filter(m => {
                if (currentLeague === 'streak') return !!m.h2h_checked;
                if (currentLeague === 'pts') return !!m.pts_outlier;
                if (currentLeague === 'form5') return !!m.form5_checked;
                return currentLeague === 'all' || m.league === currentLeague;
            });
            if (currentLeague === 'pts') {
                rows = rows.slice().sort((a, b) => Math.abs(b.pts_residual || 0) - Math.abs(a.pts_residual || 0));
            } else if (currentLeague === 'form5') {
                rows = rows.slice().sort((a, b) => {
                    const av = Math.max(Number(a.form5_home_pts) || 0, Number(a.form5_away_pts) || 0);
                    const bv = Math.max(Number(b.form5_home_pts) || 0, Number(b.form5_away_pts) || 0);
                    return bv - av || kickoffMs(a) - kickoffMs(b);
                });
            } else {
                rows = sortByKickoff(rows);
            }
            renderHead();
            if (!rows.length) {
                const emptyMsg = currentLeague === 'pts'
                    ? '같은 승점차의 평균 배당 대비 편차가 큰 경기가 없습니다.'
                    : currentLeague === 'form5'
                    ? '최근 5경기 승점이 9점 이상인 팀이 없습니다.'
                    : '표시할 축구 승무패 경기가 없습니다.';
                setStatus(`
                    <div class="no-data">
                        <div class="icon">📊</div>
                        <div>${emptyMsg}</div>
                    </div>`);
                return;
            }
            const slim = isForm5View();
            body.innerHTML = rows.map(m => `
                <tr data-league="${escapeHtml(m.league)}" class="${rowHotClass(m)}">
                    <td style="color:#888;">${escapeHtml(m.date)}</td>
                    <td><span class="league-badge ${leagueClass(m.league)}">${escapeHtml(m.league)}</span></td>
                    <td class="team-name">${escapeHtml(m.home)}</td>
                    <td class="odds odds-win ${m.fav_side === '홈' ? 'odds-lowest' : ''}">${m.win_allot}</td>
                    <td class="odds odds-draw">${m.draw_allot}</td>
                    <td class="odds odds-lose ${m.fav_side === '원정' ? 'odds-lowest' : ''}">${m.lose_allot}</td>
                    <td class="team-name">${escapeHtml(m.away)}</td>
                    <td><span class="fav-badge ${favClass(m.fav_side)}">${escapeHtml(m.fav_team)}</span></td>
                    ${slim ? '' : `
                    <td>
                        <div class="vote-text">승${m.w_pct}% 무${m.d_pct}% 패${m.l_pct}%</div>
                        <div class="vote-bar">
                            <div class="vote-w" style="width:${m.w_pct}%"></div>
                            <div class="vote-d" style="width:${m.d_pct}%"></div>
                            <div class="vote-l" style="width:${m.l_pct}%"></div>
                        </div>
                    </td>
                    <td><span class="signal-badge ${escapeHtml(m.signal_class)}">${escapeHtml(m.signal)}</span></td>
                    <td>${ptsCell(m)}</td>`}
                    <td>${form5Cell(m)}</td>
                    <td>${h2hCell(m)}</td>
                </tr>
            `).join('');
            cards.innerHTML = rows.map(matchCard).join('');
        }

        function applyData(data) {
            allMatches = data.matches || [];
            annotatePts(allMatches);
            document.getElementById('roundInfo').textContent = data.round_info || '-';
            document.getElementById('saleEnd').textContent = data.sale_end || '-';
            const cacheTag = data.cached ? ' (캐시)' : '';
            document.getElementById('fetchedAt').textContent = (data.fetched_at || '-') + cacheTag;
            document.getElementById('matchCount').textContent = allMatches.length;
            document.getElementById('leagueCount').textContent = (data.leagues || []).length;
            document.getElementById('homeFavCount').textContent = data.home_fav_count ?? 0;
            document.getElementById('awayFavCount').textContent = data.away_fav_count ?? 0;
            document.getElementById('h2hStreakCount').textContent = data.h2h_streak_count ?? 0;
            document.getElementById('ptsOutlierCount').textContent = allMatches.filter(m => m.pts_outlier).length;
            const form5Count = document.getElementById('form5Count');
            if (form5Count) {
                form5Count.textContent = data.form5_count ?? allMatches.filter(m => m.form5_checked).length;
            }
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
            setStatus(`
                    <div class="loading">
                        <div class="spinner"></div>
                        <div>${force ? '최신 배당을 다시 수집하는 중...' : '데이터를 불러오는 중입니다. 휴대폰에서는 첫 로딩이 1~2분 걸릴 수 있습니다.'}</div>
                    </div>`);
            try {
                for (let i = 0; i < 90; i++) {
                    const url = (force && i === 0) ? '/api/data?refresh=1' : '/api/data';
                    const res = await fetch(url);
                    if (!res.ok) throw new Error('HTTP ' + res.status);
                    const data = await res.json();
                    if (data.error && !data.ready && !(data.matches || []).length) throw new Error(data.error);
                    if (data.ready) {
                        applyData(data);
                        return;
                    }
                    await new Promise(resolve => setTimeout(resolve, 2000));
                }
                throw new Error('timeout');
            } catch (err) {
                const detail = err && err.message ? String(err.message) : '';
                setStatus(`
                    <div class="no-data">
                        <div class="icon">⚠️</div>
                        <div>데이터를 불러오지 못했습니다. 새로고침을 다시 눌러 주세요.</div>
                        <div class="h2h-form" style="margin-top:8px;white-space:pre-wrap;max-width:720px;margin-left:auto;margin-right:auto;">${escapeHtml(detail)}</div>
                    </div>`);
            }
        }

        document.getElementById('filterWrap').addEventListener('click', (event) => {
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

        renderHead();
        loadData(false);
    </script>
</body>
</html>
"""


def _remote_scrape_blocked():
    return False


def _blocked_message():
    url = (os.environ.get('RENDER_EXTERNAL_URL') or 'https://(렌더주소)').rstrip('/')
    return (
        '베트맨은 Render 해외 서버에서 접속이 차단됩니다.\n'
        '한국 PC에서 아래를 실행하면 이 사이트에 배당이 올라갑니다.\n\n'
        f'python scraper.py --publish {url}'
    )


def _empty_payload(pending=True, error=None):
    return {
        'ready': False,
        'pending': pending,
        'error': error,
        'matches': [],
        'leagues': [],
        'round_info': '-',
        'sale_end': '-',
        'fetched_at': '-',
        'cached': False,
        'home_fav_count': 0,
        'away_fav_count': 0,
        'h2h_streak_count': 0,
        'form5_count': 0,
        'h2h_ready': False,
    }


def _full_payload(data):
    from scraper import PROTO_FOTMOB_LEAGUE_IDS, _sort_matches

    matches = data.get('matches') or []
    # 캐시에 남은 주변 리그(노르웨이·사우디 등)는 화면에서 제외
    filtered = []
    for m in matches:
        lid = m.get('fotmob_league_id')
        try:
            lid = int(lid) if lid is not None and lid != '' else None
        except (TypeError, ValueError):
            lid = None
        if lid is not None and lid not in PROTO_FOTMOB_LEAGUE_IDS:
            continue
        league = str(m.get('league') or '')
        if league in (
            '사우디', '분데스2', '포르투갈', '리그2', '세리에B', '라리가2',
            '스코틀랜드', '벨기에', '노르웨이', '스웨덴', '덴마크', '중초',
            '아르헨티나', '브라질', '리가MX',
        ):
            continue
        filtered.append(m)
    matches = filtered
    _sort_matches(matches)
    leagues = list(dict.fromkeys(m['league'] for m in matches))
    return {
        'ready': True,
        'pending': False,
        'error': None,
        'matches': matches,
        'leagues': leagues,
        'round_info': data.get('round_info', '-'),
        'sale_end': data.get('sale_end', '-'),
        'fetched_at': data.get('fetched_at', '-'),
        'cached': data.get('cached', False),
        'home_fav_count': sum(1 for m in matches if m.get('fav_side') == '홈'),
        'away_fav_count': sum(1 for m in matches if m.get('fav_side') == '원정'),
        'h2h_streak_count': sum(1 for m in matches if m.get('h2h_checked')),
        'form5_count': sum(1 for m in matches if m.get('form5_checked')),
        'h2h_ready': bool(data.get('h2h_ready')),
    }


def _load_dump_file():
    if not os.path.isfile(DUMP_PATH):
        return None
    try:
        with open(DUMP_PATH, encoding='utf-8') as fh:
            data = json.load(fh)
        if data.get('matches'):
            with _STATE_LOCK:
                _STATE['data'] = data
                _STATE['ts'] = time.time()
                _STATE['error'] = None
            return data
        if data.get('error'):
            with _STATE_LOCK:
                _STATE['error'] = data.get('error')
            return data
    except Exception as exc:
        print('[JOB] dump read fail', exc, flush=True)
    return None


def _run_job():
    print('[JOB] scrape start', flush=True)
    log_lines = []
    try:
        scraper = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scraper.py')
        proc = subprocess.Popen(
            [sys.executable, '-u', scraper, '--dump', DUMP_PATH],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        def _read_output():
            if not proc.stdout:
                return
            for line in proc.stdout:
                print(line, end='', flush=True)
                log_lines.append(line)

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()
        while proc.poll() is None:
            _load_dump_file()
            time.sleep(1)
        reader.join(timeout=5)
        _load_dump_file()
        log_tail = ''.join(log_lines[-60:]).strip()
        if proc.returncode != 0:
            print('[JOB] scrape exit', proc.returncode, flush=True)
            with _STATE_LOCK:
                if not _STATE['data']:
                    current = _STATE.get('error') or ''
                    if current and not current.startswith('수집 실패'):
                        _STATE['error'] = current
                    elif log_tail:
                        _STATE['error'] = log_tail[-2500:]
                    else:
                        _STATE['error'] = f'수집 실패 (code {proc.returncode})'
        else:
            print('[JOB] scrape done', flush=True)
    except Exception as exc:
        print('[JOB] scrape error', exc, flush=True)
        with _STATE_LOCK:
            if not _STATE['error']:
                _STATE['error'] = str(exc)
    finally:
        with _STATE_LOCK:
            _STATE['running'] = False


def _ensure_job(force=False):
    if _remote_scrape_blocked():
        return
    now = time.time()
    with _STATE_LOCK:
        fresh = _STATE['data'] and (now - _STATE['ts']) < CACHE_TTL_SEC
        if not force and fresh:
            return
        if _STATE['running']:
            return
        if force:
            _STATE['data'] = None
            _STATE['ts'] = 0.0
            _STATE['error'] = None
            try:
                os.remove(DUMP_PATH)
            except OSError:
                pass
        _STATE['running'] = True
    threading.Thread(target=_run_job, daemon=True).start()


def _payload(force=False):
    _ensure_job(force=force)
    with _STATE_LOCK:
        data = _STATE['data']
        running = _STATE['running']
        error = _STATE['error']
    if data:
        payload = _full_payload(data)
        payload['cached'] = (not running) and (not force)
        return payload
    if _remote_scrape_blocked():
        return _empty_payload(pending=False, error=_blocked_message())
    return _empty_payload(pending=running, error=error)


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/data')
def api_data():
    force = request.args.get('refresh') == '1'
    return jsonify(_payload(force=force))


@app.route('/api/push', methods=['POST'])
def api_push():
    expected = os.environ.get('PUSH_TOKEN', '')
    got = request.headers.get('X-Push-Token', '')
    if expected and got != expected:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    if not data.get('matches'):
        return jsonify({'ok': False, 'error': 'no matches'}), 400
    with _STATE_LOCK:
        _STATE['data'] = data
        _STATE['ts'] = time.time()
        _STATE['error'] = None
        _STATE['running'] = False
    try:
        with open(DUMP_PATH, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False)
    except Exception:
        pass
    return jsonify({'ok': True, 'matches': len(data.get('matches') or [])})


@app.route('/health')
def health():
    return jsonify({'ok': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG') == '1')
