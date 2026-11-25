#!/usr/bin/env node
// flop_sign — sign a UTF-8 message with a Polkadot JSON keystore (the file `flop keys generate` writes),
// using the @polkadot/keyring that ships inside the installed @flop-labs/cli. Nothing is installed.
//
//   FLOP_KEY_PASSWORD=… node scripts/flop_sign.cjs <keystore.json> '<message>' [--password-env NAME]
//   → {"address":"5…","publicKey":"0x…","type":"ed25519","signature":"0x…"}
//
// The password is read from the environment only; the decoded pair lives for one process. Set
// FLOP_CLI_NODE_MODULES if the CLI is not installed under `npm root -g`.
"use strict";
const path = require("path");
const fs = require("fs");
const { execSync } = require("child_process");

function cliNodeModules() {
  if (process.env.FLOP_CLI_NODE_MODULES) return process.env.FLOP_CLI_NODE_MODULES;
  const candidates = ["/usr/lib/node_modules/@flop-labs/cli/node_modules", "/usr/local/lib/node_modules/@flop-labs/cli/node_modules"];
  try {
    candidates.unshift(path.join(execSync("npm root -g", { encoding: "utf8" }).trim(), "@flop-labs/cli/node_modules"));
  } catch (_) { /* npm not on PATH; fall through */ }
  for (const c of candidates) if (fs.existsSync(path.join(c, "@polkadot/keyring"))) return c;
  throw new Error("cannot find @polkadot/keyring under the installed @flop-labs/cli (set FLOP_CLI_NODE_MODULES)");
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length < 2) {
    console.error("usage: flop_sign.cjs <keystore.json> <message> [--password-env NAME]");
    return 2;
  }
  const [keystorePath, message] = args;
  const envIdx = args.indexOf("--password-env");
  const envName = envIdx >= 0 ? args[envIdx + 1] : "FLOP_KEY_PASSWORD";
  const password = process.env[envName];
  if (!password) {
    console.error(`${envName} is not set`);
    return 2;
  }
  const nm = cliNodeModules();
  const { Keyring } = require(path.join(nm, "@polkadot/keyring"));
  const { cryptoWaitReady, signatureVerify } = require(path.join(nm, "@polkadot/util-crypto"));
  const { u8aToHex, stringToU8a } = require(path.join(nm, "@polkadot/util"));
  await cryptoWaitReady();
  const json = JSON.parse(fs.readFileSync(keystorePath, "utf8"));
  const type = (json.encoding && json.encoding.content && json.encoding.content[1]) || "sr25519";
  const keyring = new Keyring({ type, ss58Format: 42 });
  const pair = keyring.addFromJson(json);
  pair.decodePkcs8(password);
  const msg = stringToU8a(message);
  const sig = pair.sign(msg);
  const check = signatureVerify(msg, sig, pair.publicKey);
  if (!check.isValid) throw new Error("self-verification of the signature failed");
  process.stdout.write(JSON.stringify({ address: pair.address, publicKey: u8aToHex(pair.publicKey), type, signature: u8aToHex(sig) }) + "\n");
  pair.lock();
  return 0;
}

main().then((rc) => process.exit(rc)).catch((e) => { console.error(String(e && e.message || e)); process.exit(1); });
