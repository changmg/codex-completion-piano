'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const sourceFile = path.join(__dirname, '../completion-piano/extension/extension.js');
const markerPath = '/remote/user/.codex/completion-piano/last-completion.json';
const connections = [
  { remoteName: 'wsl', authority: 'wsl+Ubuntu-20.04' },
  { remoteName: 'ssh-remote', authority: 'ssh-remote+test-server' },
];

function connectionTest(name, run) {
  for (const connection of connections) {
    test(`${connection.remoteName}: ${name}`, t => run(t, connection));
  }
}

function uri(remote, file) {
  return { scheme: 'vscode-remote', authority: remote, path: file,
    toString() { return `vscode-remote://${remote}${file}`; } };
}

async function createPlayer(t, options = {}) {
  const { authority, remoteName } = options;
  const directory = options.storage || await fs.mkdtemp(path.join(os.tmpdir(), 'piano-player-'));
  if (!options.storage) t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const state = {
    commands: {}, logs: [], errors: [], plays: [], reads: [],
    settings: { enabled: true, scope: 'machine', markerPaths: {}, ...options.settings },
    event: options.event === undefined ? { id: 'old', type: 'agent-turn-complete', cwd: '/project' } : options.event,
    failure: null, poll: null, timerCleared: false,
  };
  const subscriptions = [];
  const folders = [{ uri: uri(authority, '/project') }];
  const vscode = {
    Uri: { from: value => uri(value.authority, value.path) },
    env: { remoteName },
    window: {
      createOutputChannel: () => ({ appendLine: line => state.logs.push(line), dispose() {} }),
      showErrorMessage: message => state.errors.push(message),
    },
    workspace: {
      workspaceFolders: folders,
      getConfiguration: () => ({ get: (name, fallback) => state.settings[name] ?? fallback }),
      fs: { async readFile(signal) {
        state.reads.push(signal);
        if (state.failure) throw state.failure;
        if (state.event === null) throw Object.assign(new Error('No marker'), { code: 'FileNotFound' });
        return Buffer.from(typeof state.event === 'string' ? state.event : JSON.stringify(state.event));
      } },
    },
    commands: { registerCommand(name, handler) {
      state.commands[name] = handler;
      return { dispose() {} };
    } },
  };
  const childProcess = { execFile(executable, arguments_, config, callback) {
    state.plays.push({ executable, arguments_, config });
    setImmediate(() => callback(options.playbackError || null));
    return { kill() {} };
  } };
  const context = {
    module: { exports: {} }, Buffer,
    process: { platform: options.platform || 'win32', env: { SystemRoot: 'C:\\Windows' } },
    require(name) {
      if (name === 'vscode') return vscode;
      if (name === 'node:child_process') return childProcess;
      return require(name);
    },
    setInterval(callback) { state.poll = callback; return 1; },
    clearInterval() { state.timerCleared = true; },
    setTimeout, clearTimeout,
  };
  const code = (await fs.readFile(sourceFile, 'utf8')).replace(
    /^const EVENT_FILE = .*$/m, `const EVENT_FILE = ${JSON.stringify(markerPath)};`
  );
  vm.runInNewContext(code, context, { filename: 'extension.js' });
  context.module.exports.activate({
    subscriptions, globalStorageUri: { fsPath: directory },
    extension: { packageJSON: { version: '0.1.4' } },
    asAbsolutePath: file => path.join(directory, 'Windows path with $ and spaces', file),
  });
  state.settle = async () => {
    for (let index = 0; index < 300; index++) {
      const report = await state.commands['codexCompletionPiano.diagnose']();
      if (!report.polling) return report;
      await new Promise(resolve => setTimeout(resolve, 2));
    }
    throw new Error('Player did not settle');
  };
  state.tick = async () => { state.poll(); return state.settle(); };
  state.dispose = () => subscriptions.forEach(item => item.dispose());
  t.after(state.dispose);
  await state.settle();
  return state;
}

function event(id, cwd = '/project') {
  return { id, type: 'agent-turn-complete', cwd };
}

connectionTest('startup baseline is silent, a new marker plays once on Windows', async (t, connection) => {
  const player = await createPlayer(t, connection);
  assert.equal(player.plays.length, 0);
  const report = await player.settle();
  assert.equal(report.platform, 'win32');
  assert.equal(report.remoteName, connection.remoteName);
  assert.equal(report.signals[0].uri, `vscode-remote://${connection.authority}${markerPath}`);
  assert.ok(player.reads.every(signal => signal.authority === connection.authority && signal.path === markerPath));
  player.event = event('new');
  await player.tick();
  await player.tick();
  assert.equal(player.plays.length, 1);
  assert.ok(player.plays[0].executable.endsWith('powershell.exe'));
  assert.equal(player.plays[0].config.windowsHide, true);
  assert.ok(player.plays[0].config.env.CODEX_COMPLETION_PIANO_WAV.endsWith('piano.wav'));
  assert.ok(!player.plays[0].arguments_.join(' ').includes('Windows path with'));
});

connectionTest('a marker created after the initial missing file plays', async (t, connection) => {
  const player = await createPlayer(t, { ...connection, event: null });
  player.event = event('first');
  await player.tick();
  assert.equal(player.plays.length, 1);
});

connectionTest('machine scope accepts jobs elsewhere; workspace scope honors folder boundaries', async (t, connection) => {
  const player = await createPlayer(t, connection);
  player.event = event('machine', '/other/project');
  await player.tick();
  player.settings.scope = 'workspace';
  player.event = event('outside', '/project-sibling');
  await player.tick();
  assert.equal(player.plays.length, 1);
  player.event = event('inside', '/project/child');
  await player.tick();
  assert.equal(player.plays.length, 2);
});

connectionTest('disabled playback, malformed data, unknown events, and oversized markers are silent', async (t, connection) => {
  const player = await createPlayer(t, connection);
  player.settings.enabled = false;
  player.event = event('disabled');
  await player.tick();
  player.settings.enabled = true;
  for (const value of [event('disabled'), '{bad json', 'x'.repeat(4097), { id: 'ignored', type: 'other' }, null]) {
    player.event = value;
    await player.tick();
  }
  assert.equal(player.plays.length, 0);
  player.event = event('recovered');
  await player.tick();
  assert.equal(player.plays.length, 1);
});

connectionTest('windows sharing profile storage claim a host event only once', async (t, connection) => {
  const storage = await fs.mkdtemp(path.join(os.tmpdir(), 'piano-shared-'));
  t.after(() => fs.rm(storage, { recursive: true, force: true }));
  const one = await createPlayer(t, { ...connection, storage });
  const two = await createPlayer(t, { ...connection, storage });
  one.event = event('shared-event');
  two.event = one.event;
  await Promise.all([one.tick(), two.tick()]);
  assert.equal(one.plays.length + two.plays.length, 1);
  const reloaded = await createPlayer(t, { ...connection, storage, event: one.event });
  await reloaded.tick();
  assert.equal(reloaded.plays.length, 0);
});

connectionTest('per-host paths override the embedded path and diagnostics expose them', async (t, connection) => {
  const { authority } = connection;
  const custom = '/srv/custom Codex/completion-piano/last-completion.json';
  const player = await createPlayer(t, { ...connection, settings: { markerPaths: { [authority]: custom } } });
  const report = await player.settle();
  assert.equal(report.signals[0].authority, authority);
  assert.equal(report.signals[0].uri, `vscode-remote://${authority}${custom}`);
  assert.ok(player.reads.every(signal => signal.path === custom));
});

connectionTest('disconnect errors do not replay an old marker and new events recover', async (t, connection) => {
  const player = await createPlayer(t, connection);
  player.event = event('before-disconnect');
  await player.tick();
  player.failure = new Error('Remote connection disconnected');
  await player.tick();
  await player.tick();
  assert.equal(player.logs.filter(line => line.includes('Cannot process')).length, 1);
  player.failure = null;
  await player.tick();
  assert.equal(player.plays.length, 1);
  player.event = event('after-reconnect');
  await player.tick();
  assert.equal(player.plays.length, 2);
});

connectionTest('preview exposes playback failure and Linux reports the required Windows installation', async (t, connection) => {
  const failed = await createPlayer(t, { ...connection, playbackError: new Error('Audio unavailable') });
  assert.equal(await failed.commands['codexCompletionPiano.preview'](), false);
  assert.ok(failed.errors.some(message => message.includes('playback failed')));
  const linux = await createPlayer(t, { ...connection, platform: 'linux' });
  assert.equal(await linux.commands['codexCompletionPiano.preview'](), false);
  assert.ok(linux.errors.some(message => message.includes('locally in Windows')));
  assert.equal(linux.plays.length, 0);
  linux.dispose();
  assert.equal(linux.timerCleared, true);
});

test('WSL distributions and SSH hosts keep separate marker mappings and playback receipts', async t => {
  const storage = await fs.mkdtemp(path.join(os.tmpdir(), 'piano-multiple-hosts-'));
  t.after(() => fs.rm(storage, { recursive: true, force: true }));
  const hosts = [...connections, { remoteName: 'wsl', authority: 'wsl+Ubuntu-24.04' }];
  // Different WSL distributions often use identical Linux paths.
  const markerPaths = Object.fromEntries(hosts.map(host => [
    host.authority, `/srv/${host.remoteName} Codex home/completion-piano/last-completion.json`,
  ]));
  const players = await Promise.all(hosts.map(connection => createPlayer(t, {
    ...connection, storage, settings: { markerPaths },
  })));
  for (const player of players) player.event = event('same-id-on-different-hosts');
  await Promise.all(players.map(player => player.tick()));
  for (const [index, player] of players.entries()) {
    const { authority } = hosts[index];
    assert.equal(player.plays.length, 1);
    assert.ok(player.reads.every(signal =>
      signal.authority === authority && signal.path === markerPaths[authority]));
  }
});
