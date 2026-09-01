#!/usr/bin/env node
'use strict';

/**
 * Launcher for `npm start`.
 *
 * Exists for one reason: if ELECTRON_RUN_AS_NODE is set in the environment
 * (some dev tools and agent shells set it), `electron .` silently starts as a
 * plain Node process and main.js dies with "Cannot read properties of
 * undefined (reading 'whenReady')". We strip it here so the app always starts
 * as a real Electron app.
 */

const { spawn } = require('child_process');
const path = require('path');

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;
delete env.ELECTRON_NO_ATTACH_CONSOLE;

// Own flag, consumed here. Must NOT reach Electron - it treats `--debug`
// as a Node flag and refuses to start.
const argv = process.argv.slice(2).filter((a) => {
  if (a === '--ui-debug') { env.WING_UI_DEBUG = '1'; return false; }
  return true;
});

let electronPath;
try {
  electronPath = require('electron'); // from plain Node this resolves to the binary path
} catch (e) {
  console.error('Electron не встановлено. Виконайте: npm install');
  process.exit(1);
}

if (typeof electronPath !== 'string') {
  console.error('Не вдалося визначити шлях до Electron. Виконайте: npm install');
  process.exit(1);
}

const child = spawn(electronPath, ['.', ...argv], {
  cwd: path.resolve(__dirname),
  env,
  stdio: 'inherit',
  windowsHide: false,
});

child.on('close', (code) => process.exit(code === null ? 1 : code));
child.on('error', (err) => {
  console.error('Не вдалося запустити Electron:', err.message);
  process.exit(1);
});
