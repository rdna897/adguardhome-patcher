// Real DNS -> unchanged official binary -> HTTP Query Log -> production recovery helper/UI.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { createRequire } = require('node:module');
const { withOfficialServer, loadProductionRecovery, waitFor } = require('./querylog-real-api-helpers.cjs');

const source = path.resolve(process.argv[2]);
const binary = path.resolve(process.argv[3]);
const tag = process.argv[4];

async function check(environment) {
    const { base, marker, request, sendQueries, directory } = environment;
    const production = await loadProductionRecovery(source, directory);
    const { recoverLiveLogs, compareTimes, normalizeLogs, LIVE_LOG_RECOVERY_MAX_PAGES,
        LIVE_LOG_RECOVERY_MAX_DURATION_MS } = production;
    const names = rows => rows.map(row => row.question.name.replace(/\.$/, ''));
    const ordered = rows => {
        for (let index = 1; index < rows.length; index += 1) {
            assert.ok(compareTimes(rows[index - 1].time, rows[index].time) > 0,
                `Expected strictly descending real timestamps: ${rows[index - 1].time}, ${rows[index].time}`);
        }
    };
    const queryPage = async (search, olderThan, signal, responseStatus = 'all') => {
        const params = new URLSearchParams({ limit: '100', search, response_status: responseStatus });
        if (olderThan !== undefined) params.set('older_than', olderThan);
        assert.equal(params.has('offset'), false);
        const response = await (await request(`/control/querylog?${params}`, { signal })).json();
        assert.ok(Array.isArray(response.data));
        assert.ok(response.data.length <= 100);
        assert.equal(typeof response.oldest, 'string');
        for (const row of response.data) {
            assert.ok(row.question.name.includes(search), `Filter leaked ${row.question.name}`);
            assert.equal(row.status, 'NOERROR');
            assert.match(row.time, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)$/);
            if (olderThan) assert.ok(compareTimes(row.time, olderThan) < 0, 'older_than must be strictly exclusive');
        }
        ordered(response.data);
        if (response.data.length) {
            assert.equal(response.oldest, response.data.at(-1).time,
                'Nonempty page cursor must preserve the oldest returned timestamp byte-for-byte');
        }
        if (olderThan && response.oldest) {
            assert.ok(compareTimes(response.oldest, olderThan) < 0, 'Cursor must advance strictly backwards');
        }
        return response;
    };
    const collect = async (search, responseStatus = 'all') => {
        const pages = [];
        const rows = [];
        let cursor;
        for (let index = 0; index < 20; index += 1) {
            const page = await queryPage(search, cursor, undefined, responseStatus);
            pages.push({ size: page.data.length, requested: cursor, oldest: page.oldest });
            rows.push(...page.data);
            if (!page.oldest) {
                assert.equal(new Set(names(rows)).size, rows.length, 'Real cursor pages duplicated a domain');
                ordered(rows);
                return { rows, pages };
            }
            // Preserve the server's complete RFC3339Nano string without Date/ISO conversion.
            cursor = page.oldest;
        }
        assert.fail(`Real API did not exhaust within 20 pages: ${JSON.stringify(pages)}`);
    };
    const expectDomains = (rows, expected) => {
        const actual = new Set(names(rows));
        const missing = expected.filter(name => !actual.has(name));
        assert.equal(missing.length, 0,
            `Only ${actual.size}/${expected.length} expected DNS records visible; missing ${missing.slice(0, 8).join(', ')}`);
        assert.equal(actual.size, expected.length, 'Unexpected or duplicated matching queries');
    };
    const awaitRecords = async expected => waitFor(`${expected.length} real Query Log records`, async () => {
        const result = await collect(marker);
        expectDomains(result.rows, expected);
        return result;
    });
    const recovery = async (checkpoint, options = {}, responseStatus = 'all') => {
        const calls = [];
        const original = structuredClone(checkpoint);
        const signal = AbortSignal.timeout(LIVE_LOG_RECOVERY_MAX_DURATION_MS);
        const result = await recoverLiveLogs(async cursor => {
            calls.push(cursor);
            const page = await queryPage(marker, cursor, signal, responseStatus);
            return { logs: normalizeLogs(page.data), oldest: page.oldest };
        }, checkpoint, { ...options, signal });
        assert.equal(result.error, undefined, `Recovery API error: ${result.error}`);
        assert.deepEqual(checkpoint, original, 'Recovery must not mutate its input checkpoint');
        return { result, calls };
    };

    // Retain the existing official-binary static frontend and recent-stats smoke assertions.
    const index = await fs.readFile(path.join(source, 'build/static/index.html'), 'utf8');
    const mainScript = index.match(/main\.[a-f0-9]+\.js/)[0];
    assert.ok((await (await request('/')).text()).includes(mainScript));
    assert.equal(typeof (await (await request('/control/stats?recent=3600000')).json()).num_dns_queries, 'number');

    const anchor = `anchor.${marker}`;
    await sendQueries([anchor]);
    await awaitRecords([anchor]);
    const initial = await recovery(null);
    assert.equal(initial.calls.length, 1);
    assert.equal(initial.result.gap, false);
    assert.equal(initial.result.logs[0].domain, anchor);
    const checkpoint = initial.result.checkpoint;
    const rewrittenBaseline = await recovery(null, {}, 'rewritten');
    assert.deepEqual(rewrittenBaseline.result, initial.result,
        'The supported rewritten filter must include the genuine rewrite checkpoint');

    // The browser, when enabled by the existing gate, talks directly to the same official API.
    let browser;
    let closingBrowser;
    const closeBrowser = () => browser && (closingBrowser ||= browser.close());
    let page;
    let syncStatus;
    const uiRequests = [];
    const uiViolations = [];
    const uiErrors = [];
    try {
        if (process.env.BROWSER_TEST === '1') {
            const upstreamRequire = createRequire(path.join(source, 'client/package.json'));
            const { chromium, expect } = upstreamRequire('@playwright/test');
            browser = await chromium.launch({
                executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
                args: ['--no-sandbox'],
                // The environment owns signal/watchdog cleanup, including this browser.
                handleSIGINT: false, handleSIGTERM: false, handleSIGHUP: false,
            });
            environment.onCleanup(closeBrowser);
            const context = await browser.newContext({ locale: 'en-US' });
            // Network guard only: every permitted API response comes from the real binary.
            await context.route('**/*', route => {
                const url = new URL(route.request().url());
                if (url.origin !== base || url.pathname === '/control/querylog_clear') {
                    uiViolations.push(url.href);
                    return route.abort();
                }
                return route.continue();
            });
            const login = await context.request.post(`${base}/control/login`, {
                data: { name: environment.username, password: environment.password },
            });
            assert.equal(login.status(), 200, 'Disposable browser login');
            page = await context.newPage();
            page.setDefaultTimeout(10000);
            page.on('pageerror', error => uiErrors.push(error.message));
            page.on('console', message => { if (message.type() === 'error') uiErrors.push(message.text()); });
            page.on('request', request => {
                const url = new URL(request.url());

                if (url.pathname === '/control/querylog' && url.searchParams.get('limit') === '100') {
                    if (url.searchParams.get('search') !== marker
                        || url.searchParams.get('response_status') !== 'all' || url.searchParams.has('offset')) {
                        uiViolations.push(url.href);
                    }
                    uiRequests.push(url.searchParams.get('older_than'));
                }
            });
            await page.goto(`${base}/#logs?search=${marker}&response_status=all`);
            await page.bringToFront();
            await expect(page.getByRole('button', { name: 'Start Live View', exact: true })).toBeVisible();
            await page.waitForLoadState('networkidle');
            await page.clock.install();
            await page.clock.pauseAt(new Date(Date.now() + 100));
            await page.getByRole('button', { name: 'Start Live View', exact: true }).press('Enter');
            await page.clock.runFor(1000);
            syncStatus = page.locator('.logs__sync-status[role="status"]');
            await expect(syncStatus).toHaveText('Live / Synced');
            assert.equal(uiRequests.length, 1);
        }

        const burst = Array.from({ length: 251 }, (_, index) =>
            `test-${String(index + 1).padStart(4, '0')}.burst.${marker}`);
        await sendQueries(burst);
        const normal = await awaitRecords([anchor, ...burst]);
        // Verify memory + file reads (64-entry buffer), without reading or inserting log records.
        const diskLog = await fs.stat(path.join(directory, 'data/querylog.json'));
        assert.ok(diskLog.size > 0, 'Generated traffic must exercise file-backed query-log reads');
        const pagination = await collect(`burst.${marker}`);
        expectDomains(pagination.rows, burst);
        assert.deepEqual(pagination.pages.map(item => item.size), [100, 100, 51, 0]);
        assert.deepEqual(names(pagination.rows), [...burst].reverse(), 'Newest matching queries must appear first');
        assert.ok(pagination.rows.some(row => /\.\d{4,9}(?:Z|[+-]\d\d:\d\d)$/.test(row.time)), 'Sub-millisecond API precision retained');
        const boundary = pagination.rows[99];
        const afterBoundary = await queryPage(`burst.${marker}`, boundary.time);
        assert.ok(!names(afterBoundary.data).includes(boundary.question.name), 'Exact timestamp excludes its boundary record');
        assert.equal(afterBoundary.data[0].question.name, pagination.rows[100].question.name);
        console.log(`Real pagination (251 DNS queries): ${JSON.stringify(pagination.pages)}`);

        // Exercise effective status filtering, not just the no-op "all" parameter.
        // The same real records must paginate identically with rewritten, while
        // blocked must exclude them. All cursor requests retain both filters.
        const rewritten = await collect(`burst.${marker}`, 'rewritten');
        assert.deepEqual(rewritten, pagination, 'Status-filtered pagination must retain every rewrite');
        const blocked = await collect(`burst.${marker}`, 'blocked');
        assert.deepEqual(blocked.rows, [], 'Rewrite traffic must not leak through the blocked filter');
        assert.equal(blocked.pages.at(-1).oldest, '');

        const caughtUp = await recovery(checkpoint);
        assert.equal(caughtUp.result.gap, false, `Unexpected recovery gap: ${caughtUp.result.reason}`);
        assert.equal(caughtUp.calls.length, 3);
        assert.deepEqual(caughtUp.calls, [undefined, normal.pages[0].oldest, normal.pages[1].oldest]);
        assert.deepEqual(caughtUp.result.logs.map(row => row.domain), [...burst].reverse().concat(anchor));
        assert.equal(new Set(caughtUp.result.logs.map(row => row.liveKey)).size, 252);
        assert.equal(caughtUp.result.checkpoint.time, normal.rows[0].time);
        assert.notEqual(caughtUp.result.checkpoint.time, checkpoint.time);
        console.log('Production recovery: 251/251 new queries + previous checkpoint, 3 pages, ordered/deduplicated, no gap');

        const filteredCatchUp = await recovery(rewrittenBaseline.result.checkpoint, {}, 'rewritten');
        assert.deepEqual(filteredCatchUp, caughtUp, 'Production catch-up must preserve the effective status filter');
        console.log('Real status filters: rewritten -> 100,100,51,0; blocked -> 0; filtered recovery -> 251/251');

        // Reuse the proven three-page dataset to test a deliberately smaller
        // recovery budget, independently of the later production-budget overflow.
        const reducedBudget = await recovery(checkpoint, { maxPages: 2 });
        assert.deepEqual(reducedBudget.calls, caughtUp.calls.slice(0, 2));
        assert.equal(reducedBudget.result.reason, 'page-budget');
        assert.equal(reducedBudget.result.gap, true, 'A missing checkpoint must never claim continuity');
        assert.deepEqual(reducedBudget.result.logs.map(row => row.domain), [...burst].reverse().slice(0, 200));
        assert.deepEqual(reducedBudget.result.checkpoint, caughtUp.result.checkpoint,
            'Incomplete recovery must rebase to the newest observed checkpoint');
        const reducedRebase = await recovery(reducedBudget.result.checkpoint, { maxPages: 2 });
        assert.equal(reducedRebase.calls.length, 1, 'Reduced-budget gap must not repeat historical backfill');
        assert.equal(reducedRebase.result.gap, false);
        assert.deepEqual(reducedRebase.result.checkpoint, reducedBudget.result.checkpoint);
        console.log('Reduced recovery budget: exactly 2 pages / 200 rows -> page-budget gap -> 1-page rebase');

        if (page) {
            const { expect } = createRequire(path.join(source, 'client/package.json'))('@playwright/test');
            const start = uiRequests.length;
            await page.clock.runFor(1000);
            await expect(syncStatus).toHaveText('Live / Synced');
            await expect(page.getByTestId('querylog_cell')).toHaveCount(252);
            assert.equal(uiRequests.length - start, 3);
        }

        const overflow = Array.from({ length: 1100 }, (_, index) =>
            `overflow-${String(index + 1).padStart(4, '0')}.${marker}`);
        await sendQueries(overflow);
        const expected = [anchor, ...burst, ...overflow];
        const beforeGap = await awaitRecords(expected);
        const limited = await recovery(caughtUp.result.checkpoint);
        assert.equal(limited.calls.length, LIVE_LOG_RECOVERY_MAX_PAGES);
        assert.equal(limited.result.logs.length, 1000);
        assert.equal(limited.result.gap, true);
        assert.equal(limited.result.reason, 'page-budget');
        const next = await recovery(limited.result.checkpoint);
        assert.equal(next.calls.length, 1, 'Rebased polling must not repeat ten historical requests');
        assert.equal(next.result.gap, false, 'New overlap is observable; prior gap remains a lifecycle/UI warning');
        if (page) {
            const { expect } = createRequire(path.join(source, 'client/package.json'))('@playwright/test');
            const start = uiRequests.length;
            await page.clock.runFor(1000);
            await expect(syncStatus).toContainText('Gap detected');
            assert.equal(uiRequests.length - start, LIVE_LOG_RECOVERY_MAX_PAGES);
            await expect(page.getByTestId('querylog_cell')).toHaveCount(500);
            const rebaseStart = uiRequests.length;
            const rebaseResponse = page.waitForResponse(response => {
                const url = new URL(response.url());
                return url.pathname === '/control/querylog' && url.searchParams.get('limit') === '100'
                    && !url.searchParams.has('older_than');
            });
            // Availability triggers an observable Catching up -> Gap transition.
            // The final gap-only status then proves this recovery cycle finished,
            // rather than merely observing the warning from the preceding cycle.
            await page.evaluate(() => window.dispatchEvent(new Event('online')));
            await expect(syncStatus).toContainText('Catching up');
            await page.clock.runFor(1);
            const response = await rebaseResponse;
            assert.equal(response.status(), 200, 'Rebased UI poll must succeed');
            assert.equal(await response.finished(), null, 'Rebased UI response must finish successfully');
            await expect(syncStatus).toHaveText('Gap detected — some queries may not have been recovered.');
            assert.equal(uiRequests.length - rebaseStart, 1, 'Completed rebased UI cycle must fetch only the head');
            console.log('Real-API production UI: Synced after 251 arrivals; sticky Gap after 10-page overflow; next poll 1 page');
        } else {
            console.log('Real-API browser scenario not requested (BROWSER_TEST != 1); DNS/API/helper checks executed');
        }
        assert.deepEqual(uiViolations, [], 'Browser network/filter/clear constraints');
        assert.deepEqual(uiErrors, [], 'Production UI errors');
        const afterGap = await collect(marker);
        expectDomains(afterGap.rows, expected);
        assert.deepEqual(afterGap.rows.map(row => [row.question.name, row.time]),
            beforeGap.rows.map(row => [row.question.name, row.time]), 'Recovery must leave server history unchanged');
        console.log(`Real DNS integration passed: ${expected.length}/${expected.length} genuine queries verified; `
            + '10-page budget -> explicit gap -> 1-page rebase; server history unchanged');
    } catch (error) {
        if (page) console.error('Real-API browser diagnostics:', {
            url: page.url(), body: (await page.locator('body').textContent()).slice(0, 4000), uiRequests, uiViolations, uiErrors,
        });
        throw error;
    } finally {
        await closeBrowser();
    }
}

withOfficialServer(source, binary, tag, check).catch(error => { console.error(error); process.exitCode = 1; });
