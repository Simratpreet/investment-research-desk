let watchlist = [];
        let alerts = [];
        let watchlistQuery = '';

        // --- API ---
        async function fetchWatchlist() {
            const res = await fetch('/api/watchlist');
            watchlist = await res.json();
            renderWatchlist();
            renderStats();
        }

        async function fetchAlerts() {
            const res = await fetch('/api/alerts');
            alerts = await res.json();
            renderAlerts();
            renderStats();
        }

        async function addStock(ticker, exchange, notes) {
            const btn = document.querySelector('#add-form .btn-primary');
            btn.disabled = true;
            btn.textContent = 'Verifying…';
            try {
                const res = await fetch('/api/watchlist', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ticker, exchange, notes }),
                });
                const data = await res.json();
                if (res.ok) {
                    const name = data.company ? ` — ${data.company}` : '';
                    showToast(`✓ ${ticker} (${exchange})${name}`);
                    fetchWatchlist();
                } else {
                    showToast(data.error || 'Error adding stock', 'error');
                }
            } finally {
                btn.disabled = false;
                btn.textContent = 'Add stock';
            }
        }

        async function removeStock(ticker, exchange) {
            if (!confirm(`Remove ${ticker} (${exchange})?`)) return;
            const res = await fetch(`/api/watchlist/${ticker}/${exchange}`, { method: 'DELETE' });
            if (res.ok) {
                showToast(`Removed ${ticker}`);
                fetchWatchlist();
            }
        }

        async function toggleTrack(ticker, exchange, kind, enable) {
            try {
                const res = await fetch(
                    `/api/watchlist/${encodeURIComponent(ticker)}/${encodeURIComponent(exchange)}/track`,
                    {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ [kind]: enable }),
                    });
                if (!res.ok) throw new Error('request failed');
                const updated = await res.json();
                // Update local state in place and re-render (no full refetch).
                const row = watchlist.find(s =>
                    s.ticker === updated.ticker && s.exchange === updated.exchange);
                if (row) row.track = updated.track;
                renderWatchlist();
                renderStats();
                const label = kind === 'news' ? 'News' : 'Alerts';
                showToast(`${label} ${enable ? 'enabled' : 'disabled'} for ${ticker}`);
            } catch (e) {
                showToast('Could not update tracking', 'error');
            }
        }

        async function checkNow() {
            const btn = document.getElementById('check-now-btn');
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner"></span>Checking…';
            try {
                const res = await fetch('/api/check-now', { method: 'POST' });
                const data = await res.json();
                showToast(`Check complete — ${data.alerts} alert(s)`);
                fetchAlerts();
                renderStats();
            } catch (e) {
                showToast('Check failed', 'error');
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<span class="btn-symbol">↗</span>Run check';
            }
        }

        async function clearAlerts() {
            if (!confirm('Clear all alert history?')) return;
            const res = await fetch('/api/alerts', { method: 'DELETE' });
            if (res.ok) {
                alerts = [];
                renderAlerts();
                renderStats();
                showToast('History cleared');
            }
        }

        // --- Render ---
        function escapeHtml(value) {
            return String(value ?? '').replace(/[&<>'"]/g, char => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
            })[char]);
        }

        // Render untrusted markdown safely: parse, then strip any active
        // content (scripts, event handlers, javascript: URLs) via DOMPurify.
        function renderMarkdown(md) {
            return DOMPurify.sanitize(marked.parse(String(md ?? '')));
        }

        // Allowlist http(s) links only; anything else (javascript:, data:, …)
        // collapses to '#'. Caller must still escapeHtml() the result for the
        // href attribute.
        function safeUrl(value) {
            const s = String(value ?? '').trim();
            return /^https?:\/\//i.test(s) ? s : '#';
        }

        // The watchlist is a scan-list: names to monitor for news and alerts.
        // Sorted A–Z by ticker and paginated (it can run to hundreds of names).
        const WATCHLIST_PAGE_SIZE = 50;
        let watchlistPage = 1;

        function filterWatchlist() {
            watchlistQuery = document.getElementById('watchlist-search').value.trim().toLowerCase();
            watchlistPage = 1;   // a new search starts at page 1
            renderWatchlist();
        }

        function gotoWatchlistPage(p) {
            watchlistPage = p;
            renderWatchlist();
            const sec = document.querySelector('.workspace-section');
            if (sec) window.scrollTo({ top: sec.offsetTop - 72, behavior: 'smooth' });
        }

        function renderWatchlist() {
            const tbody = document.getElementById('watchlist-body');
            const pager = document.getElementById('watchlist-pagination');
            const filtered = watchlist.filter(s => {
                if (!watchlistQuery) return true;
                return [s.ticker, s.company, s.exchange]
                    .some(value => String(value || '').toLowerCase().includes(watchlistQuery));
            });
            // A–Z by ticker, case-insensitive.
            filtered.sort((a, b) => String(a.ticker || '').localeCompare(String(b.ticker || ''),
                undefined, { sensitivity: 'base' }));

            const resultCount = document.getElementById('watchlist-result-count');
            resultCount.textContent = watchlistQuery
                ? `${filtered.length} of ${watchlist.length}`
                : `${watchlist.length} names`;

            if (pager) pager.innerHTML = '';

            if (watchlist.length === 0) {
                tbody.innerHTML = `
                    <tr><td colspan="5">
                        <div class="empty-state">
                            <div class="icon">＋</div>
                            <p>Your watchlist is ready for its first stock.</p>
                        </div>
                    </td></tr>`;
                return;
            }
            if (filtered.length === 0) {
                tbody.innerHTML = `
                    <tr><td colspan="5">
                        <div class="empty-state">
                            <div class="icon">⌕</div>
                            <p>No watchlist names match “${escapeHtml(watchlistQuery)}”.</p>
                        </div>
                    </td></tr>`;
                return;
            }

            const pages = Math.max(1, Math.ceil(filtered.length / WATCHLIST_PAGE_SIZE));
            if (watchlistPage > pages) watchlistPage = pages;
            if (watchlistPage < 1) watchlistPage = 1;
            const start = (watchlistPage - 1) * WATCHLIST_PAGE_SIZE;
            const pageItems = filtered.slice(start, start + WATCHLIST_PAGE_SIZE);

            tbody.innerHTML = pageItems.map(s => {
                const added = new Date(s.added_date).toLocaleDateString('en-GB', {
                    day: 'numeric', month: 'short', year: 'numeric'
                });
                const ticker = escapeHtml(s.ticker);
                const exchange = escapeHtml(s.exchange);
                const companyName = escapeHtml(s.company || '');
                const company = companyName ? `<div class="company-name" title="${companyName}">${companyName}</div>` : '';
                const track = s.track || ['news', 'ta'];
                const newsOn = track.includes('news');
                const taOn = track.includes('ta');
                const esc = t => (t || '').replace(/'/g, "\\'");
                const toggle = (kind, on, label, title) => `
                    <button class="track-toggle ${on ? 'on' : 'off'}"
                        title="${title}: ${on ? 'on' : 'off'} — click to ${on ? 'disable' : 'enable'}"
                        aria-pressed="${on}"
                        onclick="toggleTrack('${esc(s.ticker)}', '${esc(s.exchange)}', '${kind}', ${!on})">
                        ${label}</button>`;
                return `
                    <tr>
                        <td class="ticker-cell">${ticker}${company}</td>
                        <td><span class="exchange-badge">${exchange}</span></td>
                        <td class="track-cell">
                            ${toggle('news', newsOn, 'News', 'News scanning')}
                            ${toggle('ta', taOn, 'Alerts', 'Price &amp; earnings alerts')}
                        </td>
                        <td class="date-cell">${added}</td>
                        <td>
                            <button class="btn-icon" onclick="removeStock('${esc(s.ticker)}', '${esc(s.exchange)}')" title="Remove ${ticker}" aria-label="Remove ${ticker}">✕</button>
                        </td>
                    </tr>`;
            }).join('');

            if (pager && pages > 1) {
                pager.innerHTML = `
                    <button class="btn btn-ghost btn-sm" onclick="gotoWatchlistPage(${watchlistPage - 1})" ${watchlistPage <= 1 ? 'disabled' : ''}>‹ Prev</button>
                    <span class="pagination-info">${start + 1}–${start + pageItems.length} of ${filtered.length}</span>
                    <button class="btn btn-ghost btn-sm" onclick="gotoWatchlistPage(${watchlistPage + 1})" ${watchlistPage >= pages ? 'disabled' : ''}>Next ›</button>`;
            }
        }

        // Alert type sub-tabs. `types: null` = all; otherwise the raw alert
        // types that fall under this category.
        const ALERT_CATEGORIES = [
            { key: 'all',      label: 'All',         types: null },
            { key: 'price',    label: 'Price moves', types: ['big_move'] },
            { key: 'volume',   label: 'Volume',      types: ['volume_spike'] },
            { key: 'ema',      label: 'EMA cross',   types: ['ema_crossover_bullish', 'ema_crossover_bearish'] },
            { key: 'earnings', label: 'Earnings',    types: ['earnings_imminent', 'earnings_soon'] },
        ];
        let alertFilter = 'all';
        function setAlertFilter(key) { alertFilter = key; renderAlerts(); }

        function alertNum(v, dflt) { const n = parseFloat(v); return isFinite(n) ? n : dflt; }

        // How each category ranks so the most useful alert sits on top.
        const ALERT_SORT = {
            price:    (a, b) => Math.abs(alertNum(b.change_pct, 0)) - Math.abs(alertNum(a.change_pct, 0)),
            volume:   (a, b) => alertNum(b.volume_ratio, 0) - alertNum(a.volume_ratio, 0),
            earnings: (a, b) => alertNum(a.days_until, 9999) - alertNum(b.days_until, 9999),   // soonest first
            ema:      (a, b) => String(b.timestamp || '').localeCompare(String(a.timestamp || '')),
        };

        // A compact, colour-coded magnitude chip — the sortable value made visible.
        function alertBadge(a) {
            const t = a.type || '';
            if (t === 'big_move') {
                const p = alertNum(a.change_pct, 0), up = p >= 0;
                return `<span class="alert-badge ${up ? 'up' : 'down'}">${up ? '▲' : '▼'} ${Math.abs(p).toFixed(1)}%</span>`;
            }
            if (t === 'volume_spike') {
                return `<span class="alert-badge vol">${alertNum(a.volume_ratio, 0).toFixed(1)}× vol</span>`;
            }
            if (t.startsWith('earnings')) {
                const d = alertNum(a.days_until, null);
                const lbl = d === 0 ? 'today' : d === 1 ? 'tomorrow' : d == null ? 'soon' : `in ${d}d`;
                return `<span class="alert-badge earn">${lbl}</span>`;
            }
            if (t.startsWith('ema')) {
                const bull = t.includes('bullish');
                return `<span class="alert-badge ${bull ? 'up' : 'down'}">EMA ${bull ? '▲' : '▼'}</span>`;
            }
            return '';
        }

        function alertItem(a, withWhen) {
            const when = withWhen && a.generated_at
                ? `<span class="alert-when">${escapeHtml(new Date(a.generated_at).toLocaleString('en-GB',
                    { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }))}</span>`
                : '';
            return `<div class="alert-item">
                        <div class="alert-main">${alertBadge(a)}<span class="alert-msg">${escapeHtml(a.message || '')}</span></div>
                        ${when}
                    </div>`;
        }

        function renderAlerts() {
            const container = document.getElementById('alerts-list');
            const tabsEl = document.getElementById('alert-subtabs');

            tabsEl.innerHTML = ALERT_CATEGORIES.map(c => {
                const n = c.types === null ? alerts.length
                    : alerts.filter(a => c.types.includes(a.type)).length;
                const active = c.key === alertFilter ? ' is-active' : '';
                return `<button class="alert-subtab${active}" onclick="setAlertFilter('${c.key}')">
                            ${c.label}<span class="alert-subtab-count">${n}</span>
                        </button>`;
            }).join('');

            const cat = ALERT_CATEGORIES.find(c => c.key === alertFilter) || ALERT_CATEGORIES[0];
            const filtered = cat.types === null ? alerts
                : alerts.filter(a => cat.types.includes(a.type));

            if (filtered.length === 0) {
                container.innerHTML = `
                    <div class="empty-state">
                        <div class="icon">🔔</div>
                        <p>No ${alertFilter === 'all' ? '' : cat.label.toLowerCase() + ' '}alerts yet.</p>
                    </div>`;
                return;
            }

            // A specific category ranks by what matters (biggest move, soonest
            // earnings) as one flat list, each row stamped with when it fired.
            if (cat.key !== 'all') {
                const sorted = filtered.slice().sort(ALERT_SORT[cat.key] || (() => 0));
                container.innerHTML = `<div class="alert-group">${sorted.map(a => alertItem(a, true)).join('')}</div>`;
                return;
            }

            // "All" stays chronological — grouped by the run that produced them.
            const groups = new Map();
            for (const a of filtered) {
                const key = a.generated_at || (a.timestamp || '').slice(0, 16) || 'unknown';
                if (!groups.has(key)) groups.set(key, []);
                groups.get(key).push(a);
            }
            const keys = [...groups.keys()].sort().reverse();
            container.innerHTML = keys.map(key => {
                const items = groups.get(key);
                const stamp = items[0].generated_at || items[0].timestamp;
                const when = stamp ? new Date(stamp).toLocaleString('en-GB', {
                    weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'
                }) : 'Unknown time';
                return `
                    <div class="alert-group">
                        <div class="alert-group-header">
                            <span>${when}</span>
                            <span class="alert-group-count">${items.length} alert${items.length === 1 ? '' : 's'}</span>
                        </div>
                        ${items.map(a => alertItem(a, false)).join('')}
                    </div>`;
            }).join('');
        }

        function renderStats() {
            const exchanges = {};
            let newsTracked = 0;
            let alertTracked = 0;
            watchlist.forEach(s => {
                exchanges[s.exchange] = (exchanges[s.exchange] || 0) + 1;
                const track = s.track || ['news', 'ta'];
                if (track.includes('news')) newsTracked += 1;
                if (track.includes('ta')) alertTracked += 1;
            });

            document.getElementById('stats').innerHTML = `
                <div class="stat-card primary">
                    <div class="stat-top"><div class="value">${watchlist.length}</div><span class="stat-glyph">WL</span></div>
                    <div class="label">Total names</div>
                </div>
                <div class="stat-card">
                    <div class="stat-top"><div class="value">${newsTracked}</div><span class="stat-glyph">N</span></div>
                    <div class="label">News tracked</div>
                </div>
                <div class="stat-card">
                    <div class="stat-top"><div class="value">${alertTracked}</div><span class="stat-glyph">A</span></div>
                    <div class="label">Alert enabled</div>
                </div>
                <div class="stat-card">
                    <div class="stat-top"><div class="value">${Object.keys(exchanges).length}</div><span class="stat-glyph">EX</span></div>
                    <div class="label">Exchanges</div>
                </div>`;

            document.getElementById('tab-watchlist-count').textContent = watchlist.length;
            document.getElementById('tab-alerts-count').textContent = alerts.length;
        }

        // --- Markdown Toolbar ---
        function insertMd(type) {
            const ta = document.getElementById('research-textarea');
            const start = ta.selectionStart;
            const end = ta.selectionEnd;
            const selected = ta.value.substring(start, end);
            const before = ta.value.substring(0, start);
            const after = ta.value.substring(end);

            // Ensure we're on a new line for block elements
            const needsNewline = before.length > 0 && before[before.length - 1] !== '\n';
            const nl = needsNewline ? '\n' : '';

            let insert = '';
            let cursorOffset = 0;

            switch (type) {
                case 'heading1':
                    insert = `${nl}# ${selected || 'Heading'}`;
                    cursorOffset = selected ? insert.length : nl.length + 2;
                    break;
                case 'heading2':
                    insert = `${nl}## ${selected || 'Heading'}`;
                    cursorOffset = selected ? insert.length : nl.length + 3;
                    break;
                case 'heading3':
                    insert = `${nl}### ${selected || 'Heading'}`;
                    cursorOffset = selected ? insert.length : nl.length + 4;
                    break;
                case 'bold':
                    insert = `**${selected || 'bold text'}**`;
                    cursorOffset = selected ? insert.length : 2;
                    break;
                case 'italic':
                    insert = `*${selected || 'italic text'}*`;
                    cursorOffset = selected ? insert.length : 1;
                    break;
                case 'bullet':
                    if (selected) {
                        insert = selected.split('\n').map(l => `- ${l}`).join('\n');
                    } else {
                        insert = `${nl}- `;
                    }
                    cursorOffset = insert.length;
                    break;
                case 'numbered':
                    if (selected) {
                        insert = selected.split('\n').map((l, i) => `${i + 1}. ${l}`).join('\n');
                    } else {
                        insert = `${nl}1. `;
                    }
                    cursorOffset = insert.length;
                    break;
                case 'checklist':
                    if (selected) {
                        insert = selected.split('\n').map(l => `- [ ] ${l}`).join('\n');
                    } else {
                        insert = `${nl}- [ ] `;
                    }
                    cursorOffset = insert.length;
                    break;
                case 'quote':
                    if (selected) {
                        insert = selected.split('\n').map(l => `> ${l}`).join('\n');
                    } else {
                        insert = `${nl}> `;
                    }
                    cursorOffset = insert.length;
                    break;
                case 'hr':
                    insert = `${nl}\n---\n`;
                    cursorOffset = insert.length;
                    break;
                case 'link':
                    insert = `[${selected || 'link text'}](url)`;
                    cursorOffset = selected ? insert.length - 4 : 1;
                    break;
                case 'table':
                    insert = `${nl}\n| Column 1 | Column 2 | Column 3 |\n|---|---|---|\n| | | |\n`;
                    cursorOffset = insert.length - 7;
                    break;
            }

            ta.value = before + insert + after;
            ta.focus();
            const newPos = start + cursorOffset;
            ta.selectionStart = selected ? start : newPos;
            ta.selectionEnd = selected ? start + insert.length : newPos + (type === 'bold' ? 9 : type === 'italic' ? 11 : type === 'link' ? 9 : type.startsWith('heading') ? 7 : 0);
        }

        // Tab key support in textarea
        document.addEventListener('keydown', function(e) {
            if (e.target.id === 'research-textarea' && e.key === 'Tab') {
                e.preventDefault();
                const ta = e.target;
                const start = ta.selectionStart;
                ta.value = ta.value.substring(0, start) + '  ' + ta.value.substring(ta.selectionEnd);
                ta.selectionStart = ta.selectionEnd = start + 2;
            }
        });

        // --- Research (folder-per-ticker) ---
        let researchData = [];
        let currentTicker = null;
        let currentSlug = null;

        async function fetchResearch() {
            const res = await fetch('/api/research');
            researchData = await res.json();
            renderResearchList();
        }

        function renderResearchList() {
            const grid = document.getElementById('research-grid');
            const query = (document.getElementById('research-search').value || '').toLowerCase();
            const filtered = researchData.filter(t => {
                if (!query) return true;
                if (t.ticker.toLowerCase().includes(query)) return true;
                return t.notes.some(n =>
                    n.title.toLowerCase().includes(query) ||
                    n.preview.toLowerCase().includes(query)
                );
            });

            if (filtered.length === 0) {
                grid.innerHTML = `
                    <div class="empty-state">
                        <div class="icon">📝</div>
                        <p>${query ? 'No notes match your search.' : 'No research notes yet. Click + New Note to start.'}</p>
                    </div>`;
                return;
            }

            grid.innerHTML = filtered.map(t => {
                const date = new Date(t.updated_at).toLocaleDateString('en-GB', {
                    day: 'numeric', month: 'short', year: 'numeric'
                });
                const noteNames = t.notes.map(n => n.title).join(' · ');
                return `
                    <div class="research-card" onclick="openTicker('${t.ticker}')">
                        <div class="research-card-header">
                            <span class="research-card-ticker">${t.ticker}</span>
                            <span class="research-card-date">${date}</span>
                        </div>
                        <div class="research-card-title">${t.note_count} note${t.note_count > 1 ? 's' : ''}</div>
                        <div class="research-card-preview">${noteNames}</div>
                    </div>`;
            }).join('');
        }

        function filterResearch() { renderResearchList(); }

        // Level 2: Open a ticker → show its notes
        function openTicker(ticker) {
            currentTicker = ticker;
            const tickerData = researchData.find(t => t.ticker === ticker);
            if (!tickerData) return;

            document.getElementById('research-list-view').style.display = 'none';
            document.getElementById('research-ticker-view').classList.add('active');
            document.getElementById('research-ticker-title').textContent = ticker;
            document.getElementById('research-ticker-count').textContent = `(${tickerData.note_count} note${tickerData.note_count > 1 ? 's' : ''})`;

            const grid = document.getElementById('research-notes-grid');
            grid.innerHTML = tickerData.notes.map(n => {
                const date = new Date(n.updated_at).toLocaleDateString('en-GB', {
                    day: 'numeric', month: 'short', year: 'numeric'
                });
                return `
                    <div class="research-card" onclick="openNote('${ticker}', '${n.slug}')">
                        <div class="research-card-header">
                            <span class="research-card-ticker" style="font-size:0.82rem;">${n.slug}</span>
                            <span class="research-card-date">${date}</span>
                        </div>
                        <div class="research-card-title">${n.title}</div>
                        <div class="research-card-preview">${n.preview}</div>
                    </div>`;
            }).join('');
        }

        function backToTickerList() {
            document.getElementById('research-ticker-view').classList.remove('active');
            document.getElementById('research-list-view').style.display = '';
            currentTicker = null;
            fetchResearch();
        }

        // Level 3: Open a specific note
        async function openNote(ticker, slug) {
            const res = await fetch(`/api/research/${ticker}/${slug}`);
            if (!res.ok) { showToast('Failed to load note', 'error'); return; }
            const data = await res.json();
            currentTicker = ticker;
            currentSlug = slug;

            document.getElementById('research-ticker-view').classList.remove('active');
            document.getElementById('research-note-view').classList.add('active');
            document.getElementById('research-note-title').innerHTML = `<span style="color:var(--accent);font-family:'JetBrains Mono',monospace;">${escapeHtml(ticker)}</span> / ${escapeHtml(slug)}`;

            const backBtn = document.getElementById('research-note-back');
            backBtn.textContent = `← ${ticker}`;
            backBtn.onclick = () => backToTickerFromNote();

            document.getElementById('research-rendered').innerHTML = renderMarkdown(data.content);
            document.getElementById('research-textarea').value = data.content;
            document.getElementById('research-rendered').style.display = '';
            document.getElementById('research-editor').classList.remove('active');
            document.getElementById('research-edit-btn').textContent = '✎ Edit';
        }

        function backToTickerFromNote() {
            document.getElementById('research-note-view').classList.remove('active');
            document.getElementById('research-ticker-view').classList.add('active');
            currentSlug = null;
            fetchResearch().then(() => {
                if (currentTicker) openTicker(currentTicker);
            });
        }

        function toggleResearchEdit() {
            const editor = document.getElementById('research-editor');
            const rendered = document.getElementById('research-rendered');
            const btn = document.getElementById('research-edit-btn');
            if (editor.classList.contains('active')) {
                editor.classList.remove('active');
                rendered.style.display = '';
                btn.textContent = '✎ Edit';
            } else {
                editor.classList.add('active');
                rendered.style.display = 'none';
                btn.textContent = '👁 Preview';
                document.getElementById('research-textarea').focus();
            }
        }

        function cancelResearchEdit() {
            document.getElementById('research-editor').classList.remove('active');
            document.getElementById('research-rendered').style.display = '';
            document.getElementById('research-edit-btn').textContent = '✎ Edit';
        }

        async function saveResearchNote() {
            const content = document.getElementById('research-textarea').value;
            if (!content.trim()) { showToast('Content cannot be empty', 'error'); return; }
            const res = await fetch(`/api/research/${currentTicker}/${currentSlug}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content }),
            });
            if (res.ok) {
                showToast(`Saved ${currentSlug}`);
                document.getElementById('research-rendered').innerHTML = renderMarkdown(content);
                cancelResearchEdit();
            } else {
                showToast('Failed to save', 'error');
            }
        }

        function shareNote() {
            const url = `${window.location.origin}/share/${currentTicker}/${currentSlug}`;
            navigator.clipboard.writeText(url).then(() => {
                showToast('Share link copied!');
            }).catch(() => {
                prompt('Copy this share link:', url);
            });
        }

        async function deleteCurrentNote() {
            if (!confirm(`Delete "${currentSlug}" from ${currentTicker}?`)) return;
            const res = await fetch(`/api/research/${currentTicker}/${currentSlug}`, { method: 'DELETE' });
            if (res.ok) {
                showToast(`Deleted ${currentSlug}`);
                // Go back to ticker view or list
                document.getElementById('research-note-view').classList.remove('active');
                await fetchResearch();
                const remaining = researchData.find(t => t.ticker === currentTicker);
                if (remaining) {
                    document.getElementById('research-ticker-view').classList.add('active');
                    openTicker(currentTicker);
                } else {
                    document.getElementById('research-list-view').style.display = '';
                    currentTicker = null;
                }
                currentSlug = null;
            }
        }

        async function deleteTickerNotes() {
            if (!confirm(`Delete ALL notes for ${currentTicker}?`)) return;
            const res = await fetch(`/api/research/${currentTicker}`, { method: 'DELETE' });
            if (res.ok) {
                showToast(`Deleted all notes for ${currentTicker}`);
                backToTickerList();
            }
        }

        function addNoteToTicker() {
            const slug = prompt('Note name (e.g. concall_q4_fy25, thesis, valuation):');
            if (!slug) return;
            const cleanSlug = slug.trim().toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '');
            if (!cleanSlug) { showToast('Invalid note name', 'error'); return; }
            const content = `# ${cleanSlug.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}\n\nAdd your notes here.\n`;
            fetch(`/api/research/${currentTicker}/${cleanSlug}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content }),
            }).then(res => {
                if (res.ok) {
                    showToast(`Created ${cleanSlug}`);
                    fetchResearch().then(() => openNote(currentTicker, cleanSlug));
                }
            });
        }

        function toggleNewNoteForm() {
            document.getElementById('new-note-form').classList.toggle('active');
            document.getElementById('new-research-ticker').focus();
        }

        async function createResearchNote() {
            const ticker = document.getElementById('new-research-ticker').value.trim().toUpperCase();
            const slug = document.getElementById('new-research-slug').value.trim().toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '') || 'overview';
            if (!ticker) { showToast('Enter a ticker', 'error'); return; }
            const content = `# ${ticker} — ${slug.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}\n\nAdd your notes here.\n`;
            const res = await fetch(`/api/research/${ticker}/${slug}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content }),
            });
            if (res.ok) {
                document.getElementById('new-research-ticker').value = '';
                document.getElementById('new-research-slug').value = '';
                document.getElementById('new-note-form').classList.remove('active');
                showToast(`Created ${ticker}/${slug}`);
                await fetchResearch();
                openTicker(ticker);
            } else {
                showToast('Failed to create', 'error');
            }
        }

        // --- Tabs ---
        function switchTab(tab) {
            document.querySelectorAll('.tab').forEach(t => {
                const isActive = t.dataset.tab === tab;
                t.classList.toggle('active', isActive);
                t.setAttribute('aria-selected', String(isActive));
            });

            document.getElementById('watchlist-view').style.display = tab === 'watchlist' ? '' : 'none';
            document.getElementById('dashboard-summary').style.display = tab === 'watchlist' ? '' : 'none';
            document.getElementById('watchlist-actions').style.display = tab === 'watchlist' ? '' : 'none';
            document.getElementById('alerts-view').style.display = tab === 'alerts' ? '' : 'none';
            document.getElementById('clear-alerts-btn').style.display = tab === 'alerts' ? '' : 'none';
            document.getElementById('news-view').style.display = tab === 'news' ? '' : 'none';
            document.getElementById('news-actions').style.display = tab === 'news' ? 'flex' : 'none';
            document.getElementById('announcements-view').style.display = tab === 'announcements' ? '' : 'none';
            document.getElementById('announcements-actions').style.display = tab === 'announcements' ? 'flex' : 'none';
            document.getElementById('research-view').style.display = tab === 'research' ? '' : 'none';

            // Reset research to list view when switching tabs
            if (tab === 'research') {
                document.getElementById('research-list-view').style.display = '';
                document.getElementById('research-ticker-view').classList.remove('active');
                document.getElementById('research-note-view').classList.remove('active');
                currentTicker = null;
                currentSlug = null;
                fetchResearch();
            }
            if (tab === 'alerts') fetchAlerts();
            if (tab === 'news') fetchNews();
            if (tab === 'announcements') fetchAnnouncements();
        }

        // --- Announcements (screener.in, India) ---
        let annScanPoll = null;

        async function fetchAnnouncements() {
            try {
                const res = await fetch('/api/announcements');
                const data = await res.json();
                renderAnnouncements(data);
                if (annLogOpen) fetchAnnLog();   // keep the log live while polling
                if (data.scan && data.scan.running) {
                    if (!annScanPoll) annScanPoll = setInterval(fetchAnnouncements, 5000);
                } else if (annScanPoll) {
                    clearInterval(annScanPoll); annScanPoll = null;
                }
            } catch (e) {
                document.getElementById('announcements-list').innerHTML =
                    '<div class="empty-state"><div class="icon">⚠️</div><p>Could not load announcements.</p></div>';
            }
        }

        function renderAnnouncements(data) {
            const meta = document.getElementById('announcements-meta');
            const list = document.getElementById('announcements-list');
            const scanning = data.scan && data.scan.running;
            document.getElementById('stop-ann-btn').style.display = scanning ? '' : 'none';
            document.getElementById('scan-ann-btn').style.display = scanning ? 'none' : '';
            const gen = data.generated_at
                ? new Date(data.generated_at).toLocaleString('en-GB',
                    { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
                : 'never';
            const total = (data.runs || []).reduce((n, r) => n + (r.count || 0), 0);
            meta.innerHTML = `Last scan: <strong>${gen}</strong> · ${total} announcement(s) across ${(data.runs || []).length} run(s)`
                + (scanning ? ' · <span class="scanning">⏳ scanning…</span>' : '');

            if (!(data.runs || []).length) {
                list.innerHTML = `<div class="empty-state"><div class="icon">📢</div>
                    <p>No announcements yet. Hit “Scan announcements” to pull your screener.in watchlist.</p></div>`;
                return;
            }

            list.innerHTML = data.runs.map(run => {
                const when = run.ts
                    ? new Date(run.ts).toLocaleString('en-GB',
                        { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
                    : '';
                const digestHtml = run.digest ? renderMarkdown(run.digest) : '<em>No summary.</em>';
                const items = (run.items || []).map(it => {
                    const blurb = it.blurb ? ` — <span class="ann-blurb">${escapeHtml(it.blurb)}</span>` : '';
                    return `<li><a href="${escapeHtml(safeUrl(it.url))}" target="_blank" rel="noopener">${escapeHtml(it.company)}: ${escapeHtml(it.title)}</a>${blurb}</li>`;
                }).join('');
                const sources = items
                    ? `<details class="ann-sources"><summary>${run.items.length} source filing${run.items.length === 1 ? '' : 's'}</summary><ul>${items}</ul></details>`
                    : '';
                return `
                    <div class="ann-card">
                        <div class="ann-head">
                            <span class="ann-time">${when}</span>
                            <span class="ann-count">${run.count} filing${run.count === 1 ? '' : 's'}</span>
                        </div>
                        <div class="ann-digest">${digestHtml}</div>
                        ${sources}
                    </div>`;
            }).join('');
        }

        async function scanAnnouncements() {
            const btn = document.getElementById('scan-ann-btn');
            btn.disabled = true;
            btn.textContent = 'Scanning…';
            try {
                const res = await fetch('/api/announcements/scan', { method: 'POST' });
                if (res.status === 409) {
                    showToast('A scan is already running', 'error');
                } else if (res.ok) {
                    showToast('Announcement scan started — this can take several minutes');
                }
                if (!annScanPoll) annScanPoll = setInterval(fetchAnnouncements, 5000);
                fetchAnnouncements();
            } catch (e) {
                showToast('Could not start scan', 'error');
            } finally {
                setTimeout(() => { btn.disabled = false; btn.textContent = 'Scan announcements'; }, 1500);
            }
        }

        async function stopAnnouncements() {
            const btn = document.getElementById('stop-ann-btn');
            btn.disabled = true; btn.textContent = 'Stopping…';
            try {
                const res = await fetch('/api/announcements/scan/stop', { method: 'POST' });
                showToast(res.ok ? 'Stopping scan…' : 'No scan is running', res.ok ? 'success' : 'error');
                fetchAnnouncements();
            } catch (e) {
                showToast('Could not stop scan', 'error');
            } finally {
                setTimeout(() => { btn.disabled = false; btn.textContent = '■ Stop'; }, 1500);
            }
        }

        let annLogOpen = false;
        async function toggleAnnLog() {
            annLogOpen = !annLogOpen;
            const panel = document.getElementById('announcements-log');
            const btn = document.getElementById('ann-log-btn');
            panel.style.display = annLogOpen ? 'block' : 'none';
            btn.textContent = annLogOpen ? 'Hide log' : 'View log';
            if (annLogOpen) await fetchAnnLog();
        }
        async function fetchAnnLog() {
            const panel = document.getElementById('announcements-log');
            try {
                const res = await fetch('/api/announcements/log');
                const data = await res.json();
                panel.textContent = data.exists
                    ? (data.lines.join('\n') || '(log is empty)')
                    : '(no scan.log yet — run a scan first)';
                panel.scrollTop = panel.scrollHeight;   // stick to newest
            } catch (e) {
                panel.textContent = 'Could not load log.';
            }
        }

        async function clearAnnouncements() {
            if (!confirm('Clear all announcements? (The digest.md history file is kept.)')) return;
            try {
                const res = await fetch('/api/announcements', { method: 'DELETE' });
                if (res.status === 409) {
                    showToast('A scan is running — try again after it finishes', 'error');
                    return;
                }
                if (!res.ok) throw new Error();
                showToast('Announcements cleared');
                fetchAnnouncements();
            } catch (e) {
                showToast('Could not clear announcements', 'error');
            }
        }

        // --- News ---
        let newsScanPoll = null;

        async function fetchNews() {
            try {
                const res = await fetch('/api/news');
                const data = await res.json();
                renderNews(data);
                // If a scan is running, keep polling until it finishes.
                if (data.scan && data.scan.running) {
                    if (!newsScanPoll) newsScanPoll = setInterval(fetchNews, 5000);
                } else if (newsScanPoll) {
                    clearInterval(newsScanPoll); newsScanPoll = null;
                }
            } catch (e) {
                document.getElementById('news-list').innerHTML =
                    '<div class="empty-state"><div class="icon">⚠️</div><p>Could not load news.</p></div>';
            }
        }

        function sigBadge(level) {
            if (!level) return '';
            return `<span class="sig-badge sig-${level.toLowerCase()}">${level}</span>`;
        }

        function renderNews(data) {
            const meta = document.getElementById('news-meta');
            const list = document.getElementById('news-list');
            const scanning = data.scan && data.scan.running;
            document.getElementById('stop-news-btn').style.display = scanning ? '' : 'none';
            document.getElementById('scan-news-btn').style.display = scanning ? 'none' : '';
            const gen = data.generated_at
                ? new Date(data.generated_at).toLocaleString('en-GB',
                    { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
                : 'never';
            meta.innerHTML = `Last scan: <strong>${gen}</strong> · ${data.items.length} name(s) with news`
                + (scanning ? ' · <span class="scanning">⏳ scanning…</span>' : '');

            if (!data.items.length) {
                list.innerHTML = `<div class="empty-state"><div class="icon">📰</div>
                    <p>No news yet. Hit “Scan news” to fetch the latest.</p></div>`;
                return;
            }

            list.innerHTML = data.items.map(it => {
                const when = it.scanned_at
                    ? new Date(it.scanned_at).toLocaleString('en-GB',
                        { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
                    : '';
                const articles = (it.articles || []).map(a => {
                    const ir = a.ir ? '<span class="ir-tag">IR</span>' : '';
                    const src = a.source ? `<span class="art-src">${escapeHtml(a.source)}</span>` : '';
                    return `<li>${ir}<a href="${escapeHtml(safeUrl(a.link))}" target="_blank" rel="noopener">${escapeHtml(a.title)}</a>${src}</li>`;
                }).join('');
                const summary = it.summary
                    ? `<p class="news-summary">${escapeHtml(it.summary)}</p>`
                    : `<p class="news-summary muted">⚠ ${escapeHtml(it.summary_error || 'No summary')} — headlines below.</p>`;
                const reason = it.significance_reason
                    ? `<div class="sig-reason">${escapeHtml(it.significance_reason)}</div>` : '';
                return `
                    <div class="news-card">
                        <div class="news-card-head">
                            <div>
                                <span class="news-ticker">${escapeHtml(it.ticker)}</span>
                                <span class="exchange-badge ${escapeHtml(String(it.exchange ?? '').toLowerCase())}">${escapeHtml(it.exchange)}</span>
                                ${it.company ? `<span class="news-company">${escapeHtml(it.company)}</span>` : ''}
                            </div>
                            <div class="news-card-right">
                                ${sigBadge(it.significance)}
                                <span class="news-time">${when}</span>
                            </div>
                        </div>
                        ${reason}
                        ${summary}
                        <ul class="news-articles">${articles}</ul>
                    </div>`;
            }).join('');
        }

        async function clearNews() {
            if (!confirm('Clear all news? (The reports.md history file is kept.)')) return;
            try {
                const res = await fetch('/api/news', { method: 'DELETE' });
                if (res.status === 409) {
                    showToast('A scan is running — try again after it finishes', 'error');
                    return;
                }
                if (!res.ok) throw new Error();
                showToast('News cleared');
                fetchNews();
            } catch (e) {
                showToast('Could not clear news', 'error');
            }
        }

        async function scanNews() {
            const btn = document.getElementById('scan-news-btn');
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner"></span>Scanning…';
            try {
                const res = await fetch('/api/news/scan', { method: 'POST' });
                if (res.status === 409) {
                    showToast('A scan is already running', 'error');
                } else if (res.ok) {
                    showToast('News scan started — this can take a few minutes');
                }
                if (!newsScanPoll) newsScanPoll = setInterval(fetchNews, 5000);
                fetchNews();
            } catch (e) {
                showToast('Could not start scan', 'error');
            } finally {
                setTimeout(() => {
                    btn.disabled = false;
                    btn.textContent = 'Scan news';
                }, 1500);
            }
        }

        async function stopNews() {
            const btn = document.getElementById('stop-news-btn');
            btn.disabled = true; btn.textContent = 'Stopping…';
            try {
                const res = await fetch('/api/news/scan/stop', { method: 'POST' });
                showToast(res.ok ? 'Stopping scan…' : 'No scan is running', res.ok ? 'success' : 'error');
                fetchNews();
            } catch (e) {
                showToast('Could not stop scan', 'error');
            } finally {
                setTimeout(() => { btn.disabled = false; btn.textContent = '■ Stop'; }, 1500);
            }
        }

        // --- Toast ---
        function showToast(message, type = 'success') {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            toast.className = `toast ${type}`;
            toast.textContent = message;
            container.appendChild(toast);
            setTimeout(() => toast.remove(), 3000);
        }

        // --- Form ---
        document.getElementById('add-form').addEventListener('submit', (e) => {
            e.preventDefault();
            const ticker = document.getElementById('ticker-input').value.trim();
            const exchange = document.getElementById('exchange-input').value;
            if (ticker) {
                addStock(ticker, exchange, '');
                document.getElementById('ticker-input').value = '';
                document.getElementById('ticker-input').focus();
            }
        });

        // --- Init ---
        fetchWatchlist();
        fetchAlerts();
        setInterval(() => {
            fetchWatchlist();
            fetchAlerts();
        }, 30000);
