/* table-filter.js — search box + Explorer-style column filters for record tables.
 *
 * Markup contract:
 *   <div data-filter-for="ID">            controls row: an <input type="search">,
 *                                         a [data-count] span and a [data-clear] button
 *   <table id="ID" data-noun="matter">    the table
 *   <th data-filter>                      column gets a checkbox popover of its values
 *   <th data-sort>                        column sorts A–Z / Z–A (inside the popover
 *                                         when it also filters; else click the header)
 *   <td data-sort-value="x">              sort key (defaults to the cell text)
 *   <td data-value="x">                   filter value (defaults to the cell text);
 *   <td data-values='["a","b"]'>          several values (e.g. committees)
 *   <tr data-find="...">                  search haystack (defaults to the row text)
 *   <table data-page-size="20">           paginate: Prev/Next in the controls row;
 *                                         First, Prev, the current page and its four
 *                                         nearest, Next, Last under the table (?page=N)
 *
 * Each column's options narrow to what the search and the other columns'
 * filters leave (counts included), so no option leads to an empty table.
 *
 * Filter state round-trips through the URL (?q=, and ?<column>= per column),
 * matching the hearings index. OR within a column, AND across columns.
 */
(function () {
    var GLYPH = '<svg class="colheader-filter__glyph" aria-hidden="true" width="11" height="6" viewBox="0 0 11 6"><path d="M0 0 L5.5 6 L11 0 Z" fill="currentColor"/></svg>';

    function slug(s) { return s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); }

    function cellValues(cell) {
        if (!cell) return [];
        if (cell.dataset.values) {
            try { return JSON.parse(cell.dataset.values); } catch (e) { return []; }
        }
        var v = cell.dataset.value !== undefined ? cell.dataset.value : cell.textContent;
        v = v.replace(/\s+/g, ' ').trim();
        return v ? [v] : [];
    }

    function setup(controls) {
        var table = document.getElementById(controls.dataset.filterFor);
        if (!table) return;
        var noun = table.dataset.noun || 'row';
        var plural = table.dataset.nounPlural || noun + 's';
        var input = controls.querySelector('input[type="search"]');
        var countEl = controls.querySelector('[data-count]');
        var clearBtn = controls.querySelector('[data-clear]');
        var rows = Array.prototype.slice.call(table.tBodies[0].rows);
        var empty = document.querySelector('[data-empty-for="' + table.id + '"]');
        var headers = Array.prototype.slice.call(table.tHead.rows[0].cells);
        var filters = [];
        var tbody = table.tBodies[0];
        var sortState = null;  // {col, dir}
        var sortButtons = [];
        var pageSize = parseInt(table.dataset.pageSize, 10) || 0;
        var currentPage = 1;
        var pagers = [];
        var pagesEl = null;
        var lastPage = 1;
        var wrap = table.closest('.table-scroll') || table;

        // Same markup as the hearings index: Prev/Next beside the count,
        // numbered buttons under the table.
        if (pageSize) {
            var prev = '<button type="button" class="pagination__btn" data-page-rel="prev" aria-label="Previous page">‹ Prev</button>';
            var next = '<button type="button" class="pagination__btn" data-page-rel="next" aria-label="Next page">Next ›</button>';
            var first = '<button type="button" class="pagination__btn" data-page-rel="first" aria-label="First page">« First</button>';
            var last = '<button type="button" class="pagination__btn" data-page-rel="last" aria-label="Last page">Last »</button>';
            var top = document.createElement('nav');
            top.className = 'pagination pagination--top';
            top.setAttribute('aria-label', 'Pages');
            top.innerHTML = prev + next;
            controls.appendChild(top);
            var bottom = document.createElement('nav');
            bottom.className = 'pagination';
            bottom.setAttribute('aria-label', 'Pages');
            bottom.innerHTML = first + prev + '<span class="pagination__pages"></span>' + next + last;
            wrap.parentNode.insertBefore(bottom, wrap.nextSibling);
            pagesEl = bottom.querySelector('.pagination__pages');
            pagers = [top, bottom];
            pagers.forEach(function (el) {
                el.hidden = true;
                el.addEventListener('click', function (ev) {
                    var b = ev.target.closest && ev.target.closest('button');
                    if (!b || b.disabled) return;
                    var rel = b.dataset.pageRel;
                    var p = rel === 'prev' ? currentPage - 1
                          : rel === 'next' ? currentPage + 1
                          : rel === 'first' ? 1
                          : rel === 'last' ? lastPage
                          : parseInt(b.dataset.page, 10);
                    if (!p || p === currentPage) return;
                    currentPage = p;
                    apply();
                    pushURL();
                    var t = wrap.getBoundingClientRect().top + window.pageYOffset - 16;
                    if (t < window.pageYOffset) window.scrollTo(0, t);
                });
            });
        }

        function sortKey(row, col) {
            var cell = row.cells[col];
            if (!cell) return '';
            return (cell.dataset.sortValue !== undefined ? cell.dataset.sortValue : cell.textContent)
                .replace(/\s+/g, ' ').trim().toLowerCase();
        }
        function sortBy(col, dir) {
            sortState = { col: col, dir: dir };
            var sorted = rows.slice().sort(function (a, b) {
                var ka = sortKey(a, col), kb = sortKey(b, col);
                // Blank or "—" cells sort last whichever way round.
                var ea = !ka || ka === '—', eb = !kb || kb === '—';
                if (ea !== eb) return ea ? 1 : -1;
                var c = ka.localeCompare(kb, undefined, { numeric: true });
                return dir === 'asc' ? c : -c;
            });
            sorted.forEach(function (r) { tbody.appendChild(r); });
            headers.forEach(function (h, i) {
                h.classList.toggle('is-sorting', i === col);
                h.dataset.sortDir = i === col ? dir : '';
            });
            sortButtons.forEach(function (b) {
                b.classList.toggle('is-current', +b.dataset.col === col && b.dataset.dir === dir);
            });
            currentPage = 1;
            apply();
        }

        // Plain sortable headers (no filter): click toggles A–Z / Z–A.
        headers.forEach(function (th, col) {
            if (!th.hasAttribute('data-sort') || th.hasAttribute('data-filter')) return;
            var label = th.textContent.trim();
            th.innerHTML = '<button type="button" class="colheader-sort-toggle">' + label + ' '
                + GLYPH + '</button>';
            th.querySelector('button').addEventListener('click', function () {
                var dir = sortState && sortState.col === col && sortState.dir === 'asc' ? 'desc' : 'asc';
                sortBy(col, dir);
            });
        });

        headers.forEach(function (th, col) {
            if (!th.hasAttribute('data-filter')) return;
            var label = th.textContent.trim();
            var seen = {};
            rows.forEach(function (r) {
                cellValues(r.cells[col]).forEach(function (v) { seen[v] = (seen[v] || 0) + 1; });
            });
            var values = Object.keys(seen).sort(function (a, b) {
                if (th.dataset.filter === 'count') return seen[b] - seen[a];
                return a.localeCompare(b, undefined, { numeric: true });
            });
            if (values.length < 2) return;
            th.classList.add('colheader-filter');
            th.innerHTML = '<button type="button" class="colheader-filter__trigger" aria-haspopup="true" aria-expanded="false">'
                + label + ' ' + GLYPH + '</button>'
                + '<div class="colheader-filter__popover" role="group" aria-label="Filter by ' + label.toLowerCase() + '" hidden>'
                + '<button type="button" class="colheader-filter__popover-clear" hidden><span aria-hidden="true">×</span> Clear</button></div>';
            var pop = th.querySelector('.colheader-filter__popover');
            if (th.hasAttribute('data-sort')) {
                [['asc', 'Sort A–Z'], ['desc', 'Sort Z–A']].forEach(function (d) {
                    var b = document.createElement('button');
                    b.type = 'button';
                    b.className = 'colheader-filter__sort';
                    b.textContent = d[1];
                    b.dataset.col = col;
                    b.dataset.dir = d[0];
                    b.addEventListener('click', function () { sortBy(col, d[0]); closeAll(); });
                    pop.insertBefore(b, pop.querySelector('.colheader-filter__popover-clear'));
                    sortButtons.push(b);
                });
            }
            var options = [];
            values.forEach(function (v) {
                var l = document.createElement('label');
                l.className = 'colheader-filter__option';
                var cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.value = v;
                var span = document.createElement('span');
                span.textContent = v + ' (' + seen[v] + ')';
                l.appendChild(cb);
                l.appendChild(span);
                pop.appendChild(l);
                options.push({ value: v, label: l, span: span, cb: cb });
            });
            var f = { th: th, col: col, key: slug(label), pop: pop, options: options,
                      trigger: th.querySelector('.colheader-filter__trigger'),
                      clear: pop.querySelector('.colheader-filter__popover-clear') };
            filters.push(f);

            f.trigger.addEventListener('click', function (ev) {
                ev.stopPropagation();
                var open = pop.hidden;
                closeAll();
                pop.hidden = !open;
                f.trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
            });
            pop.addEventListener('click', function (ev) { ev.stopPropagation(); });
            pop.addEventListener('change', function () { currentPage = 1; apply(); pushURL(); });
            f.clear.addEventListener('click', function () {
                setChecked(f, []);
                currentPage = 1;
                apply();
                pushURL();
            });
        });

        function closeAll() {
            filters.forEach(function (f) {
                f.pop.hidden = true;
                f.trigger.setAttribute('aria-expanded', 'false');
            });
        }
        document.addEventListener('click', closeAll);
        document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') closeAll(); });

        function checked(f) {
            return Array.prototype.slice.call(f.pop.querySelectorAll('input:checked'))
                .map(function (cb) { return cb.value; });
        }
        function setChecked(f, values) {
            Array.prototype.slice.call(f.pop.querySelectorAll('input')).forEach(function (cb) {
                cb.checked = values.indexOf(cb.value) !== -1;
            });
        }

        var hay = rows.map(function (r) {
            return (r.dataset.find || r.textContent).replace(/\s+/g, ' ').toLowerCase();
        });

        function apply() {
            var q = input ? input.value.trim().toLowerCase() : '';
            var active = filters.map(function (f) { return checked(f); });
            var shown = 0;
            var matched = [];
            var counts = filters.map(function () { return {}; });
            rows.forEach(function (r, i) {
                var searchOk = !q || hay[i].indexOf(q) !== -1;
                // Which column filters this row fails: none means it is shown;
                // exactly one means it still counts towards that column's options.
                var failed = [];
                for (var k = 0; k < filters.length; k++) {
                    if (!active[k].length) continue;
                    var vals = cellValues(r.cells[filters[k].col]);
                    if (!vals.some(function (v) { return active[k].indexOf(v) !== -1; })) failed.push(k);
                }
                if (searchOk) {
                    filters.forEach(function (f, k) {
                        if (failed.length === 0 || (failed.length === 1 && failed[0] === k)) {
                            cellValues(r.cells[f.col]).forEach(function (v) {
                                counts[k][v] = (counts[k][v] || 0) + 1;
                            });
                        }
                    });
                }
                var ok = searchOk && failed.length === 0;
                r.hidden = true;
                if (ok) { shown++; matched.push(r); }
            });
            filters.forEach(function (f, k) {
                f.options.forEach(function (o) {
                    var n = counts[k][o.value] || 0;
                    o.span.textContent = o.value + ' (' + n + ')';
                    o.label.hidden = n === 0 && !o.cb.checked;
                });
            });
            // Page through matches in their on-screen (sorted) order.
            matched.sort(function (a, b) { return a.sectionRowIndex - b.sectionRowIndex; });
            var pageCount = pageSize ? Math.max(1, Math.ceil(shown / pageSize)) : 1;
            currentPage = Math.min(Math.max(1, currentPage), pageCount);
            var from = pageSize ? (currentPage - 1) * pageSize : 0;
            var to = pageSize ? from + pageSize : shown;
            matched.forEach(function (r, i) { r.hidden = i < from || i >= to; });
            filters.forEach(function (f, k) {
                f.th.classList.toggle('is-active', active[k].length > 0);
                f.clear.hidden = active[k].length === 0;
            });
            var any = q || active.some(function (a) { return a.length; });
            if (clearBtn) clearBtn.hidden = !any;
            if (countEl) {
                countEl.textContent = pageCount > 1
                    ? 'Showing ' + (from + 1) + '–' + Math.min(to, shown) + ' of ' + shown
                    : 'Showing ' + shown + ' of ' + rows.length;
            }
            if (empty) empty.hidden = shown !== 0;
            renderPages(pageCount);
        }

        function renderPages(pageCount) {
            if (!pageSize) return;
            pagers.forEach(function (el) {
                el.hidden = pageCount <= 1;
                Array.prototype.slice.call(el.querySelectorAll('[data-page-rel]')).forEach(function (b) {
                    var back = b.dataset.pageRel === 'prev' || b.dataset.pageRel === 'first';
                    b.disabled = back ? currentPage <= 1 : currentPage >= pageCount;
                });
            });
            lastPage = pageCount;
            // The current page and its four nearest, shifted at either end.
            var start = Math.max(1, Math.min(currentPage - 2, pageCount - 4));
            var end = Math.min(pageCount, start + 4);
            var html = '';
            for (var p = start; pageCount > 1 && p <= end; p++) {
                var cur = p === currentPage;
                html += '<button type="button" class="pagination__page' + (cur ? ' is-current' : '')
                    + '" data-page="' + p + '" aria-label="Page ' + p + '"'
                    + (cur ? ' aria-current="page"' : '') + '>' + p + '</button>';
            }
            pagesEl.innerHTML = html;
        }

        function pushURL() {
            var params = new URLSearchParams(window.location.search);
            if (input) {
                if (input.value.trim()) params.set('q', input.value.trim()); else params.delete('q');
            }
            filters.forEach(function (f) {
                var c = checked(f);
                if (c.length) params.set(f.key, c.join('|')); else params.delete(f.key);
            });
            if (pageSize && currentPage > 1) params.set('page', String(currentPage));
            else params.delete('page');
            var qs = params.toString();
            history.replaceState(null, '', qs ? '?' + qs : window.location.pathname);
        }

        function readURL() {
            var params = new URLSearchParams(window.location.search);
            if (input) input.value = params.get('q') || '';
            filters.forEach(function (f) {
                setChecked(f, (params.get(f.key) || '').split('|').filter(Boolean));
            });
            currentPage = parseInt(params.get('page'), 10) || 1;
        }

        var timer;
        if (input) input.addEventListener('input', function () {
            currentPage = 1;
            apply();
            clearTimeout(timer);
            timer = setTimeout(pushURL, 150);
        });
        if (clearBtn) clearBtn.addEventListener('click', function () {
            if (input) input.value = '';
            filters.forEach(function (f) { setChecked(f, []); });
            currentPage = 1;
            apply();
            pushURL();
        });

        readURL();
        var def = headers.findIndex ? headers.findIndex(function (h) { return h.dataset.sortDefault; }) : -1;
        if (def >= 0) {
            // Rows are already in this order; just mark the header.
            sortState = { col: def, dir: headers[def].dataset.sortDefault };
            headers[def].classList.add('is-sorting');
            headers[def].dataset.sortDir = sortState.dir;
        }
        apply();
    }

    Array.prototype.slice.call(document.querySelectorAll('[data-filter-for]')).forEach(setup);
})();
