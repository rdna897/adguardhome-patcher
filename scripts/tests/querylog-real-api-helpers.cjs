// Disposable official-binary environment. No host configuration or DNS service is used.
const assert = require('node:assert/strict');
const { randomBytes } = require('node:crypto');
const dgram = require('node:dgram');
const fs = require('node:fs/promises');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { createRequire } = require('node:module');
const { setTimeout: delay } = require('node:timers/promises');

async function waitFor(label, check, timeout = 30000) {
    const end = performance.now() + timeout;
    let last;
    do {
        try { return await check(); } catch (error) { last = error; }
        await delay(50);
    } while (performance.now() < end);
    throw new Error(`${label} not ready within ${timeout}ms: ${last?.message}`, { cause: last });
}

async function withOfficialServer(source, binary, tag, test) {
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'agh-real-querylog-'));
    const cleanup = [];
    let cleaning;
    let binaryLog = '';
    let child;
    const close = () => cleaning ||= (async () => {
        const failures = [];
        for (const release of cleanup.reverse()) {
            try { await release(); } catch (error) { failures.push(error); }
        }
        try { await fs.rm(directory, { recursive: true, force: true }); } catch (error) { failures.push(error); }
        if (failures.length) throw new AggregateError(failures, 'Disposable Query Log cleanup failed');
    })();
    const interrupt = () => {
        console.error(`Disposable Query Log test interrupted or exceeded its 120s budget.\n${binaryLog}`);
        void close().finally(() => process.exit(1));
    };
    const watchdog = setTimeout(interrupt, 120000);
    process.once('SIGINT', interrupt);
    process.once('SIGTERM', interrupt);
    process.once('SIGHUP', interrupt);
    const lastResort = () => child?.kill('SIGKILL');
    process.once('exit', lastResort);
    const reserveTCP = async (port = 0) => {
        const socket = net.createServer();
        cleanup.push(async () => { if (socket.listening) await new Promise(resolve => socket.close(resolve)); });
        socket.listen({ host: '127.0.0.1', port });
        await once(socket, 'listening');
        return socket;
    };
    const reserveUDP = async () => {
        const socket = dgram.createSocket('udp4');
        let open = false;
        cleanup.push(async () => { if (open) await new Promise(resolve => socket.close(resolve)); });
        socket.on('close', () => { open = false; });
        socket.bind(0, '127.0.0.1');
        await once(socket, 'listening');
        open = true;
        return socket;
    };
    try {
        const web = await reserveTCP();
        const dns = await reserveUDP();
        const dnsTCP = await reserveTCP(dns.address().port);
        const upstream = await reserveUDP();
        const unexpectedUpstream = [];
        upstream.on('message', (message, remote) => {
            unexpectedUpstream.push(message.toString('hex'));
            // Fail closed locally if a rewrite is ever bypassed. Never forward traffic.
            const response = Buffer.from(message);
            response.writeUInt16BE(0x8182, 2); // SERVFAIL
            upstream.send(response, remote.port, remote.address);
        });
        const webPort = web.address().port;
        const dnsPort = dns.address().port;
        assert.ok(webPort > 1024 && dnsPort > 1024);
        const base = `http://127.0.0.1:${webPort}`;
        const marker = `aghgate-${randomBytes(6).toString('hex')}.invalid`;
        const username = `gate-${randomBytes(6).toString('hex')}`;
        const password = 'smoke-test-password';
        const localUpstream = `127.0.0.1:${upstream.address().port}`;
        const config = {
            schema_version: 34,
            http: { address: `127.0.0.1:${webPort}` },
            // Fixed test-only bcrypt hash of the temporary instance's password above.
            users: [{ name: username, password: '$2b$10$abcdefghijklmnopqrstuuDp.Rp4YMXloxWAkPVm/bHF5F9A9ogkS' }],
            dns: {
                bind_hosts: ['127.0.0.1'], port: dnsPort, ratelimit: 0,
                upstream_dns: [localUpstream], bootstrap_dns: [localUpstream], fallback_dns: [localUpstream],
                local_ptr_upstreams: [localUpstream], use_private_ptr_resolvers: false,
                hostsfile_enabled: false, cache_enabled: false, enable_dnssec: false,
            },
            filtering: {
                protection_enabled: true, filtering_enabled: true, rewrites_enabled: true,
                rewrites: [{ domain: `*.${marker}`, answer: '192.0.2.123', enabled: true }],
                safebrowsing_enabled: false, parental_enabled: false,
                safe_search: { enabled: false },
            },
            filters: [], whitelist_filters: [],
            clients: { runtime_sources: { rdns: false, whois: false, arp: false, dhcp: false, hosts: false } },
            querylog: { enabled: true, file_enabled: true, size_memory: 64, interval: '24h' },
            statistics: { enabled: true, interval: '24h' },
            dhcp: { enabled: false }, tls: { enabled: false },
        };
        await fs.writeFile(path.join(directory, 'AdGuardHome.yaml'), JSON.stringify(config), { mode: 0o600 });
        await fs.cp(path.join(source, 'build/static'), path.join(directory, 'build/static'), { recursive: true });
        // Release only our reservations immediately before the binary binds the same ports.
        await Promise.all([web, dnsTCP, dns].map(socket => new Promise(resolve => socket.close(resolve))));
        child = spawn(binary, ['--local-frontend', '--no-check-update', '-w', directory,
            '-c', path.join(directory, 'AdGuardHome.yaml')], { cwd: directory, stdio: ['ignore', 'pipe', 'pipe'] });
        let spawnError;
        child.on('error', error => { spawnError = error; });
        for (const stream of [child.stdout, child.stderr]) {
            stream.on('data', data => { binaryLog = (binaryLog + data).slice(-16000); });
        }
        cleanup.push(async () => {
            if (child.exitCode !== null || child.signalCode !== null || spawnError) return;
            const exited = once(child, 'exit');
            child.kill('SIGTERM');
            const force = setTimeout(() => child.kill('SIGKILL'), 3000);
            try { await exited; } finally { clearTimeout(force); }
        });
        const request = async (route, options = {}) => {
            assert.ok(route.startsWith('/control/') || route === '/', 'Only this disposable instance is reachable');
            const response = await fetch(base + route, {
                ...options, redirect: 'error',
                signal: AbortSignal.any([AbortSignal.timeout(5000), ...(options.signal ? [options.signal] : [])]),
                headers: { authorization: `Basic ${Buffer.from(`${username}:${password}`).toString('base64')}`,
                    ...options.headers },
            });
            if (response.status !== 200) {
                assert.fail(`${route}: HTTP ${response.status}: ${await response.text()}`);
            }
            return response;
        };
        await waitFor('official binary HTTP listener', async () => {
            assert.ifError(spawnError);
            assert.equal(child.exitCode, null, `binary exited: ${binaryLog}`);
            const status = await (await request('/control/status')).json();
            assert.equal(status.version, tag);
            assert.equal(status.running, true);
            assert.equal(status.dns_port, dnsPort);
            assert.deepEqual(status.dns_addresses, [`127.0.0.1:${dnsPort}`]);
        });
        console.log(`Official ${tag}: localhost web=${webPort}, DNS=${dnsPort}; isolated work/config; rate limit disabled`);
        const sender = await reserveUDP();
        await new Promise((resolve, reject) => {
            sender.connect(dnsPort, '127.0.0.1', error => error ? reject(error) : resolve());
        });
        let queryId = 0;
        const sendQueries = async names => {
            for (const name of names) {
                queryId += 1;
                const header = Buffer.alloc(12);
                header.writeUInt16BE(queryId, 0);
                header.writeUInt16BE(0x0100, 2);
                header.writeUInt16BE(1, 4);
                const question = Buffer.concat([...name.split('.').map(label =>
                    Buffer.concat([Buffer.from([label.length]), Buffer.from(label)])), Buffer.from([0, 0, 1, 0, 1])]);
                const response = once(sender, 'message', { signal: AbortSignal.timeout(2000) });
                sender.send(Buffer.concat([header, question]));
                const [answer] = await response.catch(error => {
                    throw new Error(`DNS query ${queryId} (${name}) did not receive a response`, { cause: error });
                });
                assert.ok(answer.length >= 12 + question.length + 16, `${name}: short DNS response`);
                assert.equal(answer.readUInt16BE(0), queryId, `${name}: DNS transaction ID`);
                assert.equal(answer.readUInt16BE(2) & 0x820f, 0x8000, `${name}: successful untruncated DNS response`);
                assert.equal(answer.readUInt16BE(4), 1);
                assert.equal(answer.readUInt16BE(6), 1, `${name}: one rewrite answer`);
                assert.deepEqual(answer.subarray(12, 12 + question.length), question, `${name}: echoed question`);
                // The unchanged binary compresses the answer owner to the original question.
                const offset = 12 + question.length;
                assert.equal(answer.readUInt16BE(offset), 0xc00c);
                assert.equal(answer.readUInt16BE(offset + 2), 1);
                assert.equal(answer.readUInt16BE(offset + 4), 1);
                assert.equal(answer.readUInt16BE(offset + 10), 4);
                assert.deepEqual([...answer.subarray(offset + 12, offset + 16)], [192, 0, 2, 123]);
                // Serial completion plus 1ms separation avoids relying on scheduler-created timestamp ties.
                await delay(1);
            }
        };
        await test({ base, marker, username, password, request, sendQueries, directory,
            onCleanup: release => cleanup.push(release) });
        assert.deepEqual(unexpectedUpstream, [], 'All DNS must use local rewrites; upstream trap was contacted');
        console.log('No upstream DNS requests; all DNS queries answered by local rewrites');
    } catch (error) {
        console.error(`Real Query Log integration failed in disposable ${directory}\n${binaryLog}`);
        throw error;
    } finally {
        clearTimeout(watchdog);
        await close();
        process.removeListener('SIGINT', interrupt);
        process.removeListener('SIGTERM', interrupt);
        process.removeListener('SIGHUP', interrupt);
        process.removeListener('exit', lastResort);
    }
}

async function loadProductionRecovery(source, directory) {
    const upstreamRequire = createRequire(path.join(source, 'client/package.json'));
    const { outputFiles } = await upstreamRequire('esbuild').build({
        stdin: { contents: `export * from './src/helpers/liveQueryLogRecovery';
            export { compareTimes, identifyLiveLogs } from './src/helpers/liveQueryLog';
            export { normalizeLogs } from './src/helpers/helpers';`, resolveDir: path.join(source, 'client') },
        bundle: true, platform: 'node', format: 'cjs', write: false,
    });
    const compiled = path.join(directory, 'production-recovery.cjs');
    await fs.writeFile(compiled, outputFiles[0].contents);
    return require(compiled);
}

module.exports = { withOfficialServer, loadProductionRecovery, waitFor };
