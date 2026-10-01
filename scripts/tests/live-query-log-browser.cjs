// Exercise the actual production UI with deterministic API fixtures and browser timers.
const assert = require('node:assert/strict');
const path = require('node:path');
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
    const accessWrites = [];
    let failNextLive = false;
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
        localStorage.setItem('query_log_live', 'false');
        window.queryAudit = [];
        const original = window.fetch;
        window.fetch = (...args) => {
            if (String(args[0]).includes('querylog?')) window.queryAudit.push(String(args[0]));
            return original(...args);
        };
    });
    await page.route('http://dashboard.example/**', async route => {
        const url = new URL(route.request().url());
        if (!url.pathname.startsWith('/control/')) {
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
            if (params.limit === '100' && failNextLive) {
                failNextLive = false;
                return route.fulfill({ status: 503, body: 'temporary query log failure' });
            }
            const search = params.search || '';
            const data = records.filter(row =>
                (row.question.name.includes(search) || row.client.includes(search)) &&
                (!params.older_than || row.time < params.older_than))
                .sort((left, right) => right.time.localeCompare(left.time)).slice(0, Number(params.limit));
            body = { data, oldest: data.at(-1)?.time || '' };
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

    const toggle = page.getByRole('button', { name: 'Live Query Log', exact: true });
    const rows = page.getByTestId('querylog_cell');
    const indicator = page.getByRole('button', { name: '1 new query ↑', exact: true });
    const liveCount = () => page.evaluate(() => window.queryAudit.filter(url =>
        new URL(url, location.href).searchParams.get('limit') === '100').length);
    const tick = async (ms = 1200) => {
        await page.clock.runFor(ms);
        await page.waitForLoadState('networkidle');
    };
    const visibility = state => page.evaluate(value => {
        Object.defineProperty(document, 'visibilityState', { configurable: true, value });
        document.dispatchEvent(new Event('visibilitychange'));
    }, state);
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
        await expect(toggle).toHaveText('Paused');
        await expect(rows).toHaveCount(20);
        await page.waitForLoadState('networkidle');
        await expect(page.locator('.logs__loading')).toHaveCount(1);
        await page.clock.install();
        await page.clock.pauseAt(new Date(Date.now() + 100));
        await toggle.click();
        await expect(toggle).toHaveText('Live');
        await page.waitForLoadState('networkidle');
        await waitForLiveFilter('google.com', 'blocked');
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
        await expect(toggle).toHaveText('Paused');
        const pausedCount = await liveCount();
        await tick(5000);
        await tick(500); // Let the existing success-toast exit transition complete.
        assert.equal(await liveCount(), pausedCount);
        await expect(indicator).toBeVisible();
        await indicator.click();
        await expect(rows).toHaveCount(22);
        await expect(indicator).toHaveCount(0);
        await toggle.click();
        await expect(toggle).toHaveText('Live');
        await page.waitForLoadState('networkidle');
        await page.getByRole('button', { name: 'Clear view', exact: true }).click();
        await expect(rows).toHaveCount(0);
        await tick();
        await expect(rows).toHaveCount(0);
        assert.equal(serverClears, 0);
        records.unshift(rawRow(206));
        await tick();
        await expect(rows).toHaveCount(1);

        await visibility('hidden');
        const hiddenCount = await liveCount();
        await tick(5000);
        assert.equal(await liveCount(), hiddenCount);
        records.unshift(rawRow(208));
        await visibility('visible');
        await tick(0);
        assert.equal(await liveCount(), hiddenCount + 1);
        await expect(rows).toHaveCount(2);

        failNextLive = true;
        await tick();
        await expect(page.locator('body')).toContainText('temporary query log failure');
        await expect(rows).toHaveCount(2);
        const failedCount = await liveCount();
        await tick(4000);
        assert.equal(await liveCount(), failedCount);
        records.unshift(rawRow(210));
        await tick();
        assert.equal(await liveCount(), failedCount + 1);
        await expect(rows).toHaveCount(3);

        const search = page.getByTestId('querylog_search');
        await search.fill('microsoft.com');
        await search.press('Enter');
        await page.waitForLoadState('networkidle');
        await waitForLiveFilter('microsoft.com', 'blocked');
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
        await toggle.click();
        await expect(toggle).toHaveText('Paused');
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
        await expect(toggle).toHaveText('Live');
        await page.waitForLoadState('networkidle');
        await tick();
        await page.evaluate(() => { location.hash = '#/guide'; });
        await expect(toggle).toHaveCount(0);
        const leavingCount = await liveCount();
        await tick(5000);
        assert.equal(await liveCount(), leavingCount);
        assert.equal(serverClears, 0);
        assert.deepEqual(errors, []);
        console.log(`${viewport.width}x${viewport.height}: Live/Paused, polling, queued block/unblock, pause while scrolled, Show Newest, browser-only Clear view, filters, visibility, failure/recovery, paused pagination and unmount passed`);
    } catch (error) {
        console.error('Browser diagnostics:', {
            viewport, errors, visibility: await page.evaluate(() => document.visibilityState),
            recentRequests: requests.slice(-4),
            buttons: await page.getByRole('button').allTextContents(),
            body: (await page.locator('body').innerText()).slice(0, 400),
            url: page.url(), consoleErrors: consoleErrors.slice(0, 2), endpoints: [...new Set(endpoints)],
        });
        throw error;
    } finally {
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
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
