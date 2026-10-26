'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const sourceFile = path.join(__dirname, '../completion-piano/extension/extension.js');
const markerPath = '/remote/user/.codex/completion-piano/last-completion.json';
const authority = 'ssh-remote+test-server';

function uri(remote, file) {
  return { scheme: 'vscode-remote', authority: remote, path: file,
    toString() { return `vscode-remote://${remote}${file}`; } };
}

async function createPlayer(t, options = {}) {
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
    env: { remoteName: 'ssh-remote' },
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

test('startup baseline is silent, a new marker plays once on Windows', async t => {
  const player = await createPlayer(t);
  assert.equal(player.plays.length, 0);
  player.event = event('new');
  await player.tick();
  await player.tick();
  assert.equal(player.plays.length, 1);
  assert.ok(player.plays[0].executable.endsWith('powershell.exe'));
  assert.equal(player.plays[0].config.windowsHide, true);
  assert.ok(player.plays[0].config.env.CODEX_COMPLETION_PIANO_WAV.endsWith('piano.wav'));
  assert.ok(!player.plays[0].arguments_.join(' ').includes('Windows path with'));
});

test('a marker created after the initial missing file plays', async t => {
  const player = await createPlayer(t, { event: null });
  player.event = event('first');
  await player.tick();
  assert.equal(player.plays.length, 1);
});

test('machine scope accepts jobs elsewhere; workspace scope honors folder boundaries', async t => {
  const player = await createPlayer(t);
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

test('disabled playback, malformed data, unknown events, and oversized markers are silent', async t => {
  const player = await createPlayer(t);
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

test('windows sharing profile storage claim a host event only once', async t => {
  const storage = await fs.mkdtemp(path.join(os.tmpdir(), 'piano-shared-'));
  t.after(() => fs.rm(storage, { recursive: true, force: true }));
  const one = await createPlayer(t, { storage });
  const two = await createPlayer(t, { storage });
  one.event = event('shared-event');
  two.event = one.event;
  await Promise.all([one.tick(), two.tick()]);
  assert.equal(one.plays.length + two.plays.length, 1);
  const reloaded = await createPlayer(t, { storage, event: one.event });
  await reloaded.tick();
  assert.equal(reloaded.plays.length, 0);
});

test('per-host paths override the embedded path and diagnostics expose them', async t => {
  const custom = '/srv/custom Codex/completion-piano/last-completion.json';
  const player = await createPlayer(t, { settings: { markerPaths: { [authority]: custom } } });
  const report = await player.settle();
  assert.equal(report.signals[0].authority, authority);
  assert.equal(report.signals[0].uri, `vscode-remote://${authority}${custom}`);
  assert.ok(player.reads.every(signal => signal.path === custom));
});

test('disconnect errors do not replay an old marker and new events recover', async t => {
  const player = await createPlayer(t);
  player.event = event('before-disconnect');
  await player.tick();
  player.failure = new Error('SSH disconnected');
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

test('preview exposes playback failure and Linux reports the required Windows installation', async t => {
  const failed = await createPlayer(t, { playbackError: new Error('Audio unavailable') });
  assert.equal(await failed.commands['codexCompletionPiano.preview'](), false);
  assert.ok(failed.errors.some(message => message.includes('playback failed')));
  const linux = await createPlayer(t, { platform: 'linux' });
  assert.equal(await linux.commands['codexCompletionPiano.preview'](), false);
  assert.ok(linux.errors.some(message => message.includes('locally in Windows')));
  assert.equal(linux.plays.length, 0);
  linux.dispose();
  assert.equal(linux.timerCleared, true);
});
