// Exercise the actual production UI with deterministic API fixtures and browser timers.
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs/promises');
const { createRequire } = require('node:module');

const source = path.resolve(process.argv[2] || '.');
const upstreamRequire = createRequire(path.join(source, 'client/package.json'));
const { chromium, expect } = upstreamRequire('@playwright/test');
const staticRoot = path.join(source, 'build/static');

const rawRow = (id, domain = 'google.com') => ({
    time: `2026-10-01T12:00:00.${String(id).padStart(9, '0')}Z`,
    client: id % 2 ? '192.0.2.11' : '192.0.2.10',
    question: { name: domain, type: 'A' }, reason: 'FilteredBlackList', status: 'NXDOMAIN',
    answer: [], rules: [], elapsedMs: '0.01',
    client_info: { whois: {}, name: '', disallowed: Boolean(id % 2), disallowed_rule: id % 2 ? '192.0.2.11' : '' },
});

async function check(browser, viewport) {
    const context = await browser.newContext({ viewport, locale: 'en-US' });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    const consoleErrors = [];
    const endpoints = [];
    const requests = [];
    const responses = [];
    const accessWrites = [];
    let queryEpochStart = 0;
    let failNextLive = false;
    let failNextCursor = false;
    let heldLive;
    let liveInFlight = 0;
    let maxLiveInFlight = 0;
    let serverClears = 0;
    let access = { allowed_clients: [], disallowed_clients: ['192.0.2.11'], blocked_hosts: [] };
    let records = Array.from({ length: 200 }, (_, i) => rawRow(200 - i));
    records.push(...Array.from({ length: 120 }, (_, i) => rawRow(500 - i, 'microsoft.com')));
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
    page.on('dialog', dialog => dialog.accept());
    await page.addInitScript(() => {
        // Start visible; later phases drive Page Visibility events explicitly.
        Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
        if (localStorage.getItem('query_log_live') === null) localStorage.setItem('query_log_live', 'false');
        window.queryAudit = [];
        window.errorToastCount = 0;
        const seenToasts = new WeakSet();
        new MutationObserver(() => {
            document.querySelectorAll('.toast--error').forEach(toast => {
                if (!seenToasts.has(toast)) {
                    seenToasts.add(toast);
                    window.errorToastCount += 1;
                }
            });
        }).observe(document, { childList: true, subtree: true });
        const original = window.fetch;
        window.fetch = (...args) => {
            if (String(args[0]).includes('querylog?')) window.queryAudit.push(String(args[0]));
            return original(...args);
        };
    });
    await page.route('http://dashboard.example/**', async route => {
        const url = new URL(route.request().url());
        if (!url.pathname.startsWith('/control/')) {
            if (url.pathname === '/') queryEpochStart = requests.length;
            return route.fulfill({ path: path.join(staticRoot, url.pathname === '/' ? 'index.html' : url.pathname) });
        }
        const endpoint = url.pathname.slice('/control/'.length);
        endpoints.push(endpoint);
        let body = {};
        if (endpoint === 'status') {
            body = {
                running: true, version: 'v0.107.79', protection_enabled: true, protection_disabled_duration: 0,
                dns_addresses: ['127.0.0.1'], dns_port: 53, http_port: 80, language: 'en', dhcp_available: false,
            };
        } else if (endpoint === 'profile') {
            body = { name: 'demo', language: 'en', theme: 'dark' };
        } else if (endpoint === 'tls/status') {
            body = { enabled: false, certificate_chain: '', private_key: '' };
        } else if (endpoint === 'version.json') {
            body = { new_version: 'v0.107.79', can_autoupdate: false };
        } else if (endpoint === 'querylog/config') {
            body = { enabled: true, interval: 2160, anonymize_client_ip: false };
        } else if (endpoint === 'querylog') {
            const params = Object.fromEntries(url.searchParams);
            requests.push(params);
            const live = params.limit === '100';
            if (live) maxLiveInFlight = Math.max(maxLiveInFlight, ++liveInFlight);
            try {
                if (live && (failNextLive || (failNextCursor && params.older_than))) {
                    failNextLive = false;
                    failNextCursor = false;
                    return await route.fulfill({ status: 503, body: 'temporary query log failure' });
                }
                const search = params.search || '';
                const data = records.filter(row =>
                    (row.question.name.includes(search) || row.client.includes(search)) &&
                    (!params.older_than || row.time < params.older_than))
                    .sort((left, right) => right.time.localeCompare(left.time)).slice(0, Number(params.limit));
                // Respect the server cursor, including nanoseconds; never synthesize an offset.
                body = { data, oldest: data.at(-1)?.time || '' };
                if (live && heldLive && !heldLive.seen && heldLive.matches(params)) {
                    heldLive.seen = true;
                    await heldLive.promise;
                }
                responses.push({ params, count: data.length, newest: data[0]?.time, oldest: body.oldest });
                return await route.fulfill({ json: body });
            } finally {
                if (live) liveInFlight -= 1;
            }
        } else if (endpoint === 'querylog_clear') {
            serverClears += 1;
        } else if (endpoint === 'filtering/status') {
            body = { enabled: true, filters: [], whitelist_filters: [], user_rules: [] };
        } else if (endpoint === 'clients') {
            body = { clients: [], auto_clients: [], supported_tags: [] };
        } else if (endpoint === 'blocked_services/all') {
            body = { blocked_services: [], groups: [] };
        } else if (endpoint === 'access/list') {
            body = access;
        } else if (endpoint === 'access/set') {
            access = route.request().postDataJSON();
            accessWrites.push(access);
        } else if (endpoint === 'dns_info') {
            body = { upstream_dns: [], bootstrap_dns: [], fallback_dns: [], local_ptr_upstreams: [], dnssec_enabled: false };
        }
        return route.fulfill({ json: body });
    });

    const start = page.getByRole('button', { name: 'Start Live View', exact: true });
    const pause = page.getByRole('button', { name: 'Pause Live View', exact: true });
    const toggle = page.getByRole('button', { name: /^(Start|Pause) Live View$/ });
    const clear = page.getByRole('button', { name: 'Clear view', exact: true });
    const search = page.getByTestId('querylog_search');
    const rows = page.getByTestId('querylog_cell');
    const indicator = page.getByRole('button', { name: /^\d+ new quer(?:y|ies) — Show newest$/ });
    const syncStatus = page.locator('.logs__sync-status[role="status"]');
    const synced = 'Live / Synced';
    const gapText = 'Gap detected — some queries may not have been recovered.';
    const screenshot = async (state) => {
        if (!process.env.LIVE_LOG_SCREENSHOT_DIR) return;
        await fs.mkdir(process.env.LIVE_LOG_SCREENSHOT_DIR, { recursive: true });
        await page.screenshot({ path: path.join(process.env.LIVE_LOG_SCREENSHOT_DIR,
            `query-log-${viewport.width < 768 ? `mobile-${viewport.width}` : 'desktop'}-${state}.png`), fullPage: false });
    };
    const checkControls = async (active) => {
        await expect(active ? pause : start).toBeVisible();
        await expect(active ? pause : start).toHaveAccessibleName(active ? 'Pause Live View' : 'Start Live View');
        await expect(active ? start : pause).toHaveCount(0);
        await expect(clear).toHaveCount(active ? 1 : 0);
        assert.equal(await toggle.getAttribute('aria-pressed'), null);
        assert.equal(await toggle.isEnabled(), true);
        await expect(syncStatus).toHaveCount(1);
        await expect(syncStatus).toHaveAttribute('aria-live', 'polite');
        await expect(syncStatus).toHaveAttribute('aria-atomic', 'true');
        if (active) {
            await expect(clear).toHaveAccessibleDescription('Clear only the displayed browser Live View. AdGuard Home’s server Query Log and history are kept.');
            await expect(clear).toHaveClass(/btn-outline-secondary/);
            const primaryBox = await pause.boundingBox();
            const clearBox = await clear.boundingBox();
            assert.equal(primaryBox.y, clearBox.y, 'Live actions stay on one row');
            assert.ok(clearBox.x >= primaryBox.x + primaryBox.width + 7, 'Actions have a visible gap');
        }
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        // Exercise both palettes at every width without producing new screenshot artifacts.
        const originalTheme = await page.locator('body').getAttribute('data-theme');
        for (const theme of ['light', 'dark']) {
            await page.locator('body').evaluate((element, value) => element.setAttribute('data-theme', value), theme);
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
                `${viewport.width}px ${theme}: controls/status fit the viewport`);
            const box = await syncStatus.boundingBox();
            assert.ok(box.width > 0 && box.x >= 0 && box.x + box.width <= viewport.width,
                `${viewport.width}px ${theme}: sync status stays within the viewport`);
        }
        await page.locator('body').evaluate((element, value) => {
            if (value === null) element.removeAttribute('data-theme');
            else element.setAttribute('data-theme', value);
        }, originalTheme);
    };
    const liveCount = () => page.evaluate(() => window.queryAudit.filter(url =>
        new URL(url, location.href).searchParams.get('limit') === '100').length);
    const tick = async (ms = 1200) => {
        await page.clock.runFor(ms);
        // Fetch dispatch and Node's route callback cross a transport boundary. An already
        // satisfied networkidle state can otherwise return before the new request starts.
        const sent = await page.evaluate(() => window.queryAudit.length);
        await expect.poll(() => requests.length - queryEpochStart).toBeGreaterThanOrEqual(sent);
        await page.waitForLoadState('networkidle');
    };
    const nextPoll = async () => {
        const before = await liveCount();
        // A response schedules the next timer after crossing the real transport boundary.
        // Advancing virtual time once can finish a prior poll without starting the next one.
        for (let attempt = 0; attempt < 3; attempt += 1) {
            await tick();
            if (await liveCount() > before) return;
        }
        assert.fail('Expected a new Live poll after advancing the browser clock');
    };
    const visibility = state => page.evaluate(value => {
        Object.defineProperty(document, 'visibilityState', { configurable: true, value });
        document.dispatchEvent(new Event('visibilitychange'));
    }, state);
    const online = value => page.evaluate(available => {
        // A deterministic navigator/event fixture: routed requests still use the local static fixture.
        Object.defineProperty(navigator, 'onLine', { configurable: true, value: available });
        window.dispatchEvent(new Event(available ? 'online' : 'offline'));
    }, value);
    const holdLive = (matches = params => Boolean(params.older_than)) => {
        assert.ok(!heldLive, 'Only one response may be held');
        let release;
        const promise = new Promise(resolve => { release = resolve; });
        heldLive = { matches, promise, release, seen: false };
    };
    const releaseLive = async () => {
        assert.ok(heldLive?.seen, 'The intended request reached the route fixture');
        heldLive.release();
        heldLive = undefined;
        await page.waitForLoadState('networkidle');
    };
    const beginHeldPoll = async () => {
        // A cancelled prior cycle can settle after the first virtual timer fires.
        // Advance in bounded steps, giving real transport callbacks time to finish.
        for (let attempt = 0; attempt < 3; attempt += 1) {
            await page.clock.runFor(1200);
            try {
                await expect.poll(() => heldLive.seen, { timeout: 500 }).toBe(true);
                return;
            } catch (error) {
                if (attempt === 2) throw error;
            }
        }
    };
    let newestId = 1000;
    const arrive = count => {
        const added = Array.from({ length: count }, () => {
            newestId += 1;
            return rawRow(newestId, `q-${newestId}.google.com`);
        }).reverse();
        records.unshift(...added);
        return added;
    };
    const renderedDomains = () => rows.locator('.logs__cell--domain .overflow-break-mobile[title]')
        .evaluateAll(elements => elements.map(element => element.getAttribute('title')));
    const expectNewest = async added => {
        await expect(rows).toHaveCount(added.length);
        assert.deepEqual(await renderedDomains(), added.map(row => row.question.name),
            'Recovered rows are newest first, complete, and contain no duplicates');
    };
    const assertPages = (requestStart, responseStart, count) => {
        const actual = requests.slice(requestStart);
        const pages = responses.slice(responseStart);
        assert.equal(actual.length, count, 'Recovery stops as soon as the newest checkpoint group overlaps');
        assert.equal(pages.length, count);
        assert.equal(actual[0].older_than, undefined, 'Each cycle starts at the newest page');
        actual.forEach((params, index) => {
            assert.equal(params.limit, '100');
            assert.equal(params.offset, undefined);
            assert.equal(params.search, 'google.com');
            assert.equal(params.response_status, 'blocked');
            if (index) assert.equal(params.older_than, pages[index - 1].oldest,
                'Sequential recovery follows the previous server-provided oldest cursor');
        });
        assert.equal(maxLiveInFlight, 1, 'Live page requests never overlap');
    };
    const waitForLiveFilter = async (search, status) => {
        // A filter's normal server fetch must finish before the one-second live timer starts.
        for (let attempt = 0; attempt < 3; attempt += 1) {
            await tick();
            const latestUrl = await page.evaluate(() => window.queryAudit.at(-1));
            const latest = new URL(latestUrl, 'http://dashboard.example/').searchParams;
            if (latest.get('limit') === '100' && latest.get('search') === search && latest.get('response_status') === status) {
                // Browser fetch dispatch precedes Node's route callback; await that transport boundary.
                await expect.poll(() => {
                    const request = requests.at(-1);
                    return request.limit === '100' && request.search === search && request.response_status === status;
                }).toBe(true);
                await page.waitForLoadState('networkidle');
                return;
            }
        }
        assert.deepEqual(requests.at(-1), { search, response_status: status, limit: '100' });
    };
    try {
        await page.goto('http://dashboard.example/#/logs?search=google.com&response_status=blocked');
        await page.bringToFront();
        await expect(start).toBeVisible();
        await checkControls(false);
        await expect(syncStatus).toHaveText('Paused');
        await expect(rows).toHaveCount(20);
        await page.waitForLoadState('networkidle');
        await expect(page.locator('.logs__loading')).toHaveCount(1);
        await expect(start).toHaveClass(/btn-primary/);
        await start.focus();
        await expect(start).toBeFocused();
        assert.notEqual(await start.evaluate(el => getComputedStyle(el).boxShadow), 'none');
        await screenshot('inactive');
        await page.clock.install();
        await page.clock.pauseAt(new Date(Date.now() + 100));
        await start.press('Enter');
        await checkControls(true);
        assert.equal(await page.evaluate(() => localStorage.getItem('query_log_live')), 'true');
        await screenshot('live');
        await pause.press('Tab');
        await expect(clear).toBeFocused();
        assert.notEqual(await clear.evaluate(el => getComputedStyle(el).boxShadow), 'none');
        await page.waitForLoadState('networkidle');
        await waitForLiveFilter('google.com', 'blocked');
        await expect(syncStatus).toHaveText(synced);
        assert.ok(await liveCount() >= 1);
        await expect(rows).toHaveCount(20); // The larger live page is not new historical backfill.
        assert.equal(requests.at(-1).search, 'google.com');
        assert.equal(requests.at(-1).response_status, 'blocked');
        records.unshift(rawRow(202));
        await tick();
        await expect(rows).toHaveCount(21);
        await page.evaluate(() => {
            window.scrollTo(0, document.body.scrollHeight);
            // Deliver the scroll event explicitly while animation frames use virtual time.
            window.dispatchEvent(new Event('scroll'));
        });
        await expect.poll(() => page.evaluate(() => scrollY)).toBeGreaterThan(0);
        const scrollBefore = await page.evaluate(() => scrollY);
        records.unshift(rawRow(204));
        await tick();
        await expect(indicator).toBeVisible();
        await expect(indicator).toHaveAccessibleName('1 new query — Show newest');
        await expect(indicator).toHaveAttribute('title', /waiting while you read older rows/);
        const indicatorBox = await indicator.boundingBox();
        assert.ok(indicatorBox.x >= 0 && indicatorBox.x + indicatorBox.width <= viewport.width);
        assert.equal(await indicator.evaluate(el => el.scrollWidth > el.clientWidth), false);
        records.unshift(...Array.from({ length: 16 }, (_, i) => rawRow(206 + i * 2)));
        await tick();
        await expect(indicator).toHaveAccessibleName('17 new queries — Show newest');
        const pendingBox = await indicator.boundingBox();
        assert.ok(pendingBox.x >= 0 && pendingBox.x + pendingBox.width <= viewport.width);
        assert.equal(await indicator.evaluate(el => el.scrollWidth > el.clientWidth), false);
        await expect(rows).toHaveCount(21);
        await screenshot('pending');
        await expect(rows).toHaveCount(21);
        assert.equal(await page.evaluate(() => scrollY), scrollBefore);

        // Both existing row-action entry points must retain the queued arrival.
        for (const [index, name, writes] of [[19, 'Disallow this client', 1], [20, 'Allow this client', 2]]) {
            const row = rows.nth(index);
            if (viewport.width < 768) {
                await row.click();
            } else {
                await row.locator('.logs__cell--client .button-action__container > button').click();
            }
            await page.getByRole('button', { name, exact: true }).click();
            await expect.poll(() => accessWrites.length).toBe(writes);
            await expect(indicator).toBeVisible();
            await expect(indicator).toHaveAccessibleName('17 new queries — Show newest');
            await expect(rows).toHaveCount(21);
        }
        assert.deepEqual(accessWrites[0].disallowed_clients, ['192.0.2.11', '192.0.2.10']);
        assert.deepEqual(accessWrites[1].disallowed_clients, ['192.0.2.10']);

        // Stay away from the header without exposing the paused pagination sentinel.
        await page.evaluate(() => {
            const table = document.querySelector('.logs__table');
            window.scrollTo(0, table.getBoundingClientRect().top + scrollY + 100);
            window.dispatchEvent(new Event('scroll'));
        });
        // Invoke the header toggle without Playwright scrolling the viewport to it.
        await toggle.evaluate(element => element.click());
        await expect(start).toBeVisible();
        await expect(clear).toHaveCount(0);
        assert.equal(await page.evaluate(() => localStorage.getItem('query_log_live')), 'false');
        const pausedCount = await liveCount();
        await tick(5000);
        await tick(500); // Let the existing success-toast exit transition complete.
        assert.equal(await liveCount(), pausedCount);
        await expect(indicator).toBeVisible();
        await indicator.press('Enter');
        await expect(rows).toHaveCount(38);
        await expect(indicator).toHaveCount(0);
        await toggle.click();
        await expect(pause).toBeVisible();
        await page.waitForLoadState('networkidle');
        await checkControls(true);
        await clear.focus();
        await expect(clear).toBeFocused();
        await clear.press('Space');
        await expect(rows).toHaveCount(0);
        await nextPoll();
        await expect(rows).toHaveCount(0);
        assert.equal(serverClears, 0);
        records.unshift(rawRow(238));
        await nextPoll();
        await expect(rows).toHaveCount(1);

        await visibility('hidden');
        await expect(syncStatus).toHaveText('Suspended');
        const hiddenCount = await liveCount();
        await tick(5000);
        assert.equal(await liveCount(), hiddenCount);
        records.unshift(rawRow(240));
        await visibility('visible');
        await tick(0);
        assert.equal(await liveCount(), hiddenCount + 1);
        await expect(rows).toHaveCount(2);

        failNextLive = true;
        const firstOutageToasts = await page.evaluate(() => window.errorToastCount);
        await tick();
        await expect(page.locator('body')).toContainText('temporary query log failure');
        await expect(syncStatus).toHaveText('Reconnecting');
        assert.equal(await page.evaluate(() => window.errorToastCount), firstOutageToasts + 1);
        await expect(rows).toHaveCount(2);
        const failedCount = await liveCount();
        await expect(pause).toBeEnabled(); // A failed poll must still be pausable.
        await tick(4000);
        assert.equal(await liveCount(), failedCount);
        failNextLive = true;
        await tick();
        assert.equal(await liveCount(), failedCount + 1);
        await expect(syncStatus).toHaveText('Reconnecting');
        assert.equal(await page.evaluate(() => window.errorToastCount), firstOutageToasts + 1,
            'Repeated failures in one outage do not create repeated error toasts');
        records.unshift(rawRow(242));
        await tick(5000);
        assert.equal(await liveCount(), failedCount + 2);
        await expect(rows).toHaveCount(3);
        await expect(syncStatus).toHaveText(synced);

        // A cleared browser view retains its recovery checkpoint and all server history.
        const clearLocalView = async () => {
            const retained = records.slice();
            await clear.click();
            await expect(rows).toHaveCount(0);
            await expect(indicator).toHaveCount(0);
            await nextPoll();
            await expect(rows).toHaveCount(0);
            assert.deepEqual(records, retained, 'Clear view must not mutate server records');
            assert.equal(serverClears, 0);
        };
        await clearLocalView();

        // The anchor is the newest identity group, not the oldest row of the baseline page.
        for (const [size, pageCount] of [[1, 1], [99, 1], [100, 2], [250, 3], [500, 6]]) {
            const requestStart = requests.length;
            const responseStart = responses.length;
            const added = arrive(size);
            await nextPoll();
            await expect(syncStatus).toHaveText(synced);
            await expectNewest(added);
            assertPages(requestStart, responseStart, pageCount);
            await nextPoll();
            await expectNewest(added); // Overlap on the following poll cannot duplicate rows.
            await clearLocalView();
        }

        // Freeze visible rows while reading; recovered pending rows retain the same 500-row cap.
        const visible = arrive(500);
        await nextPoll();
        await expectNewest(visible);
        await page.evaluate(() => {
            const table = document.querySelector('.logs__table');
            window.scrollTo(0, table.getBoundingClientRect().top + scrollY + 100);
            window.dispatchEvent(new Event('scroll'));
        });
        const readingScroll = await page.evaluate(() => scrollY);
        const pending = arrive(500);
        holdLive();
        const pendingRequestStart = requests.length;
        const pendingResponseStart = responses.length;
        await beginHeldPoll();
        await expect(syncStatus).toHaveText('Catching up');
        await expectNewest(visible);
        const heldCount = await liveCount();
        await page.clock.runFor(1000);
        assert.equal(await liveCount(), heldCount, 'No second poll starts during an in-flight cursor request');
        await releaseLive();
        await expect(indicator).toHaveAccessibleName('500 new queries — Show newest');
        assertPages(pendingRequestStart, pendingResponseStart, 6);
        await expectNewest(visible);
        assert.equal(await page.evaluate(() => scrollY), readingScroll);
        const latestPending = arrive(1);
        await nextPoll();
        await expectNewest(visible);
        await expect(indicator).toHaveAccessibleName('500 new queries — Show newest');
        await nextPoll();
        await expect(indicator).toHaveAccessibleName('500 new queries — Show newest');
        await indicator.click();
        await expectNewest([...latestPending, ...pending].slice(0, 500));
        await clearLocalView();

        // Pausing and browser availability changes retain the checkpoint across 250 arrivals.
        for (const lifecycle of ['pause', 'hidden', 'offline']) {
            if (lifecycle === 'pause') await pause.click();
            else if (lifecycle === 'hidden') await visibility('hidden');
            else await online(false);
            await expect(syncStatus).toHaveText({ pause: 'Paused', hidden: 'Suspended', offline: 'Reconnecting' }[lifecycle]);
            const stoppedCount = await liveCount();
            const added = arrive(250);
            await tick(5000);
            assert.equal(await liveCount(), stoppedCount, `${lifecycle} suppresses polling`);
            await expect(rows).toHaveCount(0);
            holdLive();
            if (lifecycle === 'pause') await start.click();
            else if (lifecycle === 'hidden') await visibility('visible');
            else await online(true);
            await beginHeldPoll();
            await expect(syncStatus).toHaveText('Catching up');
            await releaseLive();
            await expect(syncStatus).toHaveText(synced);
            await expectNewest(added);
            await nextPoll();
            await expectNewest(added);
            await clearLocalView();
        }

        // Failure after the newest page keeps its checkpoint: retry fills missing older arrivals.
        const interrupted = arrive(250);
        const catchUpToasts = await page.evaluate(() => window.errorToastCount);
        failNextCursor = true;
        await nextPoll();
        await expect(syncStatus).toHaveText('Reconnecting');
        await expect(rows).toHaveCount(100);
        assert.equal(await page.evaluate(() => window.errorToastCount), catchUpToasts + 1);
        failNextCursor = true;
        await tick(5000);
        await expect(syncStatus).toHaveText('Reconnecting');
        await expect(rows).toHaveCount(100);
        assert.equal(await page.evaluate(() => window.errorToastCount), catchUpToasts + 1);
        await tick(5000);
        await expect(syncStatus).toHaveText(synced);
        await expectNewest(interrupted);
        await clearLocalView();

        // A late response containing already displayed records must not refill a cleared view.
        const beforeClear = arrive(250);
        await nextPoll();
        await expectNewest(beforeClear);
        const retainedBeforeClear = records.slice();
        holdLive(params => !params.older_than);
        await beginHeldPoll();
        await clear.click();
        await expect(rows).toHaveCount(0);
        await releaseLive();
        await nextPoll();
        await expect(rows).toHaveCount(0);
        await expect(indicator).toHaveCount(0);
        assert.deepEqual(records, retainedBeforeClear);
        assert.equal(serverClears, 0);
        const afterClear = arrive(1);
        await nextPoll();
        await expectNewest(afterClear);
        await clearLocalView();

        // The ten-page budget is bounded, leaves a sticky warning, and rebases to the newest page.
        const gapToasts = await page.evaluate(() => window.errorToastCount);
        const overflow = arrive(1200);
        const overflowRequestStart = requests.length;
        const overflowResponseStart = responses.length;
        await nextPoll();
        assertPages(overflowRequestStart, overflowResponseStart, 10);
        await expect(syncStatus).toHaveText(gapText);
        await expectNewest(overflow.slice(0, 500));
        await checkControls(true); // Also exercise the long gap text at 320/390px in both themes.
        const rebasedRequests = requests.length;
        const rebasedResponses = responses.length;
        await nextPoll();
        assertPages(rebasedRequests, rebasedResponses, 1);
        await expect(syncStatus).toHaveText(gapText);
        await expectNewest(overflow.slice(0, 500));
        await clearLocalView();
        await expect(syncStatus).toHaveText(gapText);
        assert.equal(await page.evaluate(() => window.errorToastCount), gapToasts,
            'Page-budget gaps use the status warning without adding error toasts');

        // Rebaseline before the time-budget case so an earlier sticky gap cannot mask a failure.
        for (const value of ['microsoft.com', 'google.com']) {
            await search.fill(value);
            await search.press('Enter');
            await page.waitForLoadState('networkidle');
            await waitForLiveFilter(value, 'blocked');
            await expect(syncStatus).toHaveText(synced);
            await expect(syncStatus).not.toContainText(gapText);
        }
        await clearLocalView();

        // Transport time counts against the five-second cycle budget as well as page count.
        const timeBudgetToasts = await page.evaluate(() => window.errorToastCount);
        const timeBudgetRequests = requests.length;
        arrive(250);
        holdLive();
        await beginHeldPoll();
        await expect(syncStatus).toHaveText('Catching up');
        await page.clock.runFor(5001);
        await releaseLive();
        // A timed-out transport may also report Reconnecting until the next successful poll.
        await expect(syncStatus).toContainText(gapText);
        assert.equal(requests.length - timeBudgetRequests, 2, 'The elapsed budget prevents a third page');
        const deadlineToasts = await page.evaluate(() => window.errorToastCount);
        assert.ok(deadlineToasts <= timeBudgetToasts + 1, 'A deadline may emit at most one outage toast');
        const timeRebaseRequests = requests.length;
        const timeRebaseResponses = responses.length;
        await nextPoll();
        assertPages(timeRebaseRequests, timeRebaseResponses, 1);
        await expect(syncStatus).toHaveText(gapText);
        assert.equal(await page.evaluate(() => window.errorToastCount), deadlineToasts,
            'A budget gap cannot produce repeated outage toasts');

        await search.fill('microsoft.com');
        await search.press('Enter');
        await page.waitForLoadState('networkidle');
        await waitForLiveFilter('microsoft.com', 'blocked');
        await expect(syncStatus).toHaveText(synced); // A filter change establishes a fresh baseline.
        await expect(syncStatus).not.toContainText(gapText);
        assert.equal(requests.at(-1).limit, '100');
        assert.equal(requests.at(-1).search, 'microsoft.com');
        assert.equal(requests.at(-1).response_status, 'blocked');
        await expect(rows).toHaveCount(20);
        await search.fill('192.0.2.10');
        await search.press('Enter');
        await page.locator('select.custom-select--logs').selectOption('all');
        await page.waitForLoadState('networkidle');
        await waitForLiveFilter('192.0.2.10', 'all');
        assert.equal(requests.at(-1).search, '192.0.2.10');
        assert.equal(requests.at(-1).response_status, 'all');

        await search.fill('google.com');
        await search.press('Enter');
        await page.waitForLoadState('networkidle');
        await waitForLiveFilter('google.com', 'all');
        await pause.press('Space');
        await checkControls(false);
        assert.equal(await page.evaluate(() => localStorage.getItem('query_log_live')), 'false');
        await expect(rows).toHaveCount(20);
        await page.waitForLoadState('networkidle');
        const loader = page.locator('.logs__loading');
        await expect(loader).toHaveCount(1);
        const pagination = page.waitForResponse(response =>
            response.url().includes('/control/querylog?') && Boolean(new URL(response.url()).searchParams.get('older_than')));
        await loader.scrollIntoViewIfNeeded();
        await page.evaluate(() => window.dispatchEvent(new Event('scroll')));
        await tick();
        await pagination;
        await expect(rows).toHaveCount(40);
        assert.equal(requests.at(-1).limit, '20');

        await toggle.click();
        await expect(pause).toBeVisible();
        await page.waitForLoadState('networkidle');
        await tick();
        await page.reload();
        await page.waitForLoadState('networkidle');
        await checkControls(true); // The persisted Live preference restores the Pause action.
        await tick();
        await page.evaluate(() => { location.hash = '#/guide'; });
        await expect(toggle).toHaveCount(0);
        const leavingCount = await liveCount();
        await tick(5000);
        assert.equal(await liveCount(), leavingCount);
        assert.equal(serverClears, 0);
        assert.deepEqual(errors, []);
        console.log(`${viewport.width}x${viewport.height}: light/dark controls, sync states, cursor recovery (1/99/100/250/500/1200), caps/pending, pause/visibility/offline catch-up, outage deduplication, clear races, sticky gap/rebaseline, filters, paused pagination and unmount passed`);
    } catch (error) {
        console.error('Browser diagnostics:', {
            viewport, errors, visibility: await page.evaluate(() => document.visibilityState),
            recentRequests: requests.slice(-4),
            recentResponses: responses.slice(-4),
            liveRequests: await liveCount(), scrollY: await page.evaluate(() => scrollY),
            buttons: await page.getByRole('button').allTextContents(),
            body: (await page.locator('body').innerText()).slice(0, 400),
            url: page.url(), consoleErrors: consoleErrors.slice(0, 2), endpoints: [...new Set(endpoints)],
        });
        throw error;
    } finally {
        heldLive?.release();
        await context.close();
    }
}

(async () => {
    const browser = await chromium.launch({
        headless: true,
        ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}),
        args: ['--no-sandbox'],
    });
    try {
        await check(browser, { width: 1440, height: 900 });
        await check(browser, { width: 390, height: 844 });
        await check(browser, { width: 320, height: 740 });
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
