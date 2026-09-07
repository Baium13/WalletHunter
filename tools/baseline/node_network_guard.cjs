'use strict';
// Defense in depth for trusted baseline tests; CI also requires --network none.
if (process.env.WALLETHUNTER_BASELINE_CHILD !== '1') throw new Error('Baseline child marker missing');
const denied = {network:0, process:0, filesystem:0};
function block(kind) { return function () { denied[kind]++; throw new Error(`BASELINE_DENIED_${kind.toUpperCase()}`); }; }
const network = block('network'), processDenied = block('process');
const net = require('node:net');
net.connect = net.createConnection = net.createServer = network;
net.Socket.prototype.connect = network;
net.Server.prototype.listen = network;
const tls = require('node:tls'); tls.connect = tls.createServer = network;
for (const name of ['node:http','node:https']) {
  const mod = require(name); mod.request = mod.get = mod.createServer = network;
  if (mod.Agent) mod.Agent.prototype.createConnection = network;
}
const dns = require('node:dns');
for (const key of Object.keys(dns)) if (typeof dns[key] === 'function') dns[key] = network;
for (const key of Object.keys(dns.promises)) if (typeof dns.promises[key] === 'function') dns.promises[key] = network;
const dgram = require('node:dgram'); dgram.createSocket = network;
const child = require('node:child_process');
for (const name of ['exec','execFile','execFileSync','execSync','fork','spawn','spawnSync']) child[name] = processDenied;
if (child.ChildProcess) child.ChildProcess.prototype.spawn = processDenied;
require('node:worker_threads').Worker = processDenied;
require('node:cluster').fork = processDenied;
process.dlopen = processDenied;
globalThis.fetch = async function () { return network(); };
globalThis.WebSocket = network;
const fs = require('node:fs');
for (const name of ['writeFile','writeFileSync','appendFile','appendFileSync','rename','renameSync',
  'unlink','unlinkSync','rm','rmSync','rmdir','rmdirSync','mkdir','mkdirSync','createWriteStream',
  'truncate','truncateSync','symlink','symlinkSync','link','linkSync']) if (fs[name]) fs[name] = block('filesystem');
for (const name of ['writeFile','appendFile','rename','unlink','rm','rmdir','mkdir','truncate','symlink','link'])
  if (fs.promises[name]) fs.promises[name] = block('filesystem');
require('node:module').syncBuiltinESMExports();
Object.defineProperty(globalThis, '__BASELINE_GUARD__', {value: {active:true,denied}, writable:false});
