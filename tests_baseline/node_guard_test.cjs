'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
test('network guard is installed and only dummy environment is present',()=>{
  assert.equal(globalThis.__BASELINE_GUARD__.active,true);
  assert.equal(process.env.HL_MODE,'TESTNET'); assert.equal(process.env.AUTO_TRADING,'false');
  assert.equal(process.env.TELEGRAM_BOT_TOKEN,'000000000:baseline-fake-token');
  assert.equal(process.env.HTTPS_PROXY,undefined);
});
test('TCP DNS HTTP and HTTPS are rejected synchronously before transport',()=>{
  for(const call of [()=>require('node:net').connect(443,'api.hyperliquid.xyz'),
    ()=>require('node:dns').lookup('api.hyperliquid.xyz',()=>{}),
    ()=>require('node:http').get('http://api.hyperliquid.xyz'),
    ()=>require('node:https').request('https://api.hyperliquid-testnet.xyz/info')])
    assert.throws(call,/BASELINE_DENIED_NETWORK/);
});
test('fetch is blocked for either exchange origin',async()=>{
  await assert.rejects(fetch('https://api.hyperliquid.xyz/info'),/BASELINE_DENIED_NETWORK/);
  await assert.rejects(fetch('https://api.hyperliquid-testnet.xyz/info'),/BASELINE_DENIED_NETWORK/);
});
test('subprocess and workers cannot escape',()=>{
  assert.throws(()=>require('node:child_process').spawnSync(process.execPath,['-v']),/BASELINE_DENIED_PROCESS/);
  assert.throws(()=>new(require('node:worker_threads').Worker)('baseline.js'),/BASELINE_DENIED_PROCESS/);
});
test('unexpected fixture file mutation is blocked',()=>{
  assert.throws(()=>require('node:fs').writeFileSync('must-not-exist','blocked'),/BASELINE_DENIED_FILESYSTEM/);
});
