"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  ensureWindowsSourceNativeCore,
  publicReleaseUrl,
  validateWindowsSourceRuntimeDirectory,
} = require("../src/windows-source-native-core-bootstrap.cjs");
const {
  applySourceRuntimeEnvironment,
  ensureSourceNativeCore,
} = require("../src/source-native-core-bootstrap.cjs");
const {
  WINDOWS_NATIVE_ASR_ABI_VERSION,
  WINDOWS_NATIVE_ASR_AUTHORIZATION,
  WINDOWS_NATIVE_ASR_EXPORTS,
  WINDOWS_NATIVE_ASR_FEATURE_BIT,
  WINDOWS_NATIVE_ASR_TARGET,
} = require("../src/windows-native-asr-capability.cjs");
const { buildWindowsPeWithExports } = require("./pe-export-fixture.cjs");

const NOW_UNIX = 1786162000;
const ISSUED_AT_UNIX = NOW_UNIX - 60;
const EXPIRES_AT_UNIX = ISSUED_AT_UNIX + 45 * 24 * 60 * 60;
const ARCHIVE_CONTENT = Buffer.from("fixture-windows-source-runtime", "utf8");

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

const NATIVE_MANIFEST = Object.freeze({
  schemaVersion: 2,
  distributionMode: "public",
  buildId: "wcdb-windows-source-fixture-abcd1234",
  buildIssuedAtUnix: ISSUED_AT_UNIX,
  buildExpiresAtUnix: EXPIRES_AT_UNIX,
  readOnlyBuild: true,
  wechatActions: [],
  developmentBuild: false,
  offlineBootstrapFeatureBits: 3,
  offlineExportSealFormat: "WES2",
  codeSignatureEnforced: true,
  rootPublicKeyCompiled: true,
  testHooksEnabled: false,
  stagingPinnedSignerTrust: false,
  windowsSignerTrustMode: "private-pki",
  windowsPrivatePkiLeafRevocation: "build-and-lease-only",
  windowsClientSignerSha256: "11".repeat(32),
  windowsBrokerSignerSha256: "22".repeat(32),
  windowsPrivateRootSha256: "33".repeat(32),
  securityNoticeId: "WCE-AUTOMATED-ANALYSIS-NOTICE-V2",
  securityNoticeSha256: "44".repeat(32),
  securityCheckpointSetId: "WCE-AI-CHECKPOINT-SET-V3",
  securityCheckpointCount: 7,
  securityCheckpointSetSha256: "55".repeat(32),
  sourceRuntime: true,
  windowsHostVerification: "same-user-direct-parent",
  nativeAsrAbiVersion: WINDOWS_NATIVE_ASR_ABI_VERSION,
  nativeAsrAuthorization: WINDOWS_NATIVE_ASR_AUTHORIZATION,
  nativeAsrFeatureBit: WINDOWS_NATIVE_ASR_FEATURE_BIT,
  nativeAsrTarget: WINDOWS_NATIVE_ASR_TARGET,
});

const PAYLOADS = new Map([
  ["native-core/wechatdb_client.dll", buildWindowsPeWithExports(WINDOWS_NATIVE_ASR_EXPORTS)],
  ["native-core/wechatdb_broker.exe", Buffer.from("broker")],
  [
    "native-core/wechatdb_native_build.json",
    Buffer.from(`${JSON.stringify(NATIVE_MANIFEST, null, 2)}\n`),
  ],
]);

const RUNTIME_MANIFEST = Object.freeze({
  schemaVersion: 1,
  profile: "windows-source-public",
  platform: "win32",
  architecture: "x64",
  releaseTag: "windows-source-runtime-fixture-abcd1234",
  createdAtUnix: NOW_UNIX - 30,
  expiresAtUnix: EXPIRES_AT_UNIX,
  components: {
    nativeCore: {
      artifactName: "wechatdb-native-windows-x64-source-public",
      buildId: NATIVE_MANIFEST.buildId,
      sourceRevision: "11".repeat(20),
      producerRunId: 101,
      expiresAtUnix: EXPIRES_AT_UNIX,
      path: "native-core",
    },
  },
  files: Object.fromEntries(
    [...PAYLOADS].map(([name, value]) => [
      name,
      { sha256: sha256(value), size: value.length, executable: name.endsWith(".exe") },
    ])
  ),
});
const RUNTIME_MANIFEST_RAW = Buffer.from(`${JSON.stringify(RUNTIME_MANIFEST, null, 2)}\n`);
const PIN = Object.freeze({
  schemaVersion: 2,
  platform: "win32",
  architecture: "x64",
  publisherRepository: "LifeArchiveProject/WeChatDataAnalysis",
  releaseTag: RUNTIME_MANIFEST.releaseTag,
  assetName: "wechatdataanalysis-windows-source-runtime-x64-v1.tar.gz",
  assetSha256: sha256(ARCHIVE_CONTENT),
  runtimeManifestSha256: sha256(RUNTIME_MANIFEST_RAW),
  expiresAtUnix: EXPIRES_AT_UNIX,
});
const ARCHIVE_NAMES = [
  "./",
  "./runtime-manifest.json",
  "./native-core/",
  "./native-core/wechatdb_client.dll",
  "./native-core/wechatdb_broker.exe",
  "./native-core/wechatdb_native_build.json",
];

function writeConfig(root, pin = PIN) {
  const file = path.join(root, "pin.json");
  fs.writeFileSync(file, `${JSON.stringify(pin, null, 2)}\n`);
  return file;
}

function writeRuntime(
  root,
  { payloads = PAYLOADS, runtimeManifestRaw = RUNTIME_MANIFEST_RAW } = {}
) {
  for (const [relative, content] of payloads) {
    const file = path.join(root, ...relative.split("/"));
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, content);
  }
  fs.writeFileSync(path.join(root, "runtime-manifest.json"), runtimeManifestRaw);
}

function fixtureTools() {
  let downloads = 0;
  const spawnSyncImpl = (command, args, options) => {
    if (command === "curl.exe") {
      downloads += 1;
      assert.equal(options.shell, false);
      assert.equal(options.env.GH_TOKEN, undefined);
      assert.equal(args.at(-1), publicReleaseUrl(PIN));
      fs.writeFileSync(args[args.indexOf("--output") + 1], ARCHIVE_CONTENT);
      return { status: 0, stdout: "", stderr: "" };
    }
    if (command === "tar.exe" && args[0] === "-tzf") {
      return { status: 0, stdout: `${ARCHIVE_NAMES.join("\n")}\n`, stderr: "" };
    }
    if (command === "tar.exe" && args[0] === "-tvzf") {
      return {
        status: 0,
        stdout: `${ARCHIVE_NAMES.map((name) => `${name.endsWith("/") ? "d" : "-"}rwx fixture ${name}`).join("\n")}\n`,
        stderr: "",
      };
    }
    if (command === "tar.exe" && args[0] === "-xzf") {
      writeRuntime(args[args.indexOf("-C") + 1]);
      return { status: 0, stdout: "", stderr: "" };
    }
    throw new Error(`unexpected command: ${command} ${args.join(" ")}`);
  };
  return { spawnSyncImpl, get downloads() { return downloads; } };
}

function withTempRoot(callback) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "wda-windows-source-runtime-"));
  try {
    return callback(root);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

test("Windows source bootstrap downloads publicly once and reuses verified cache", () =>
  withTempRoot((root) => {
    const tools = fixtureTools();
    const options = {
      arch: "x64",
      cacheRoot: path.join(root, "cache"),
      configPath: writeConfig(root),
      env: { GH_TOKEN: "must-not-leak", GITHUB_TOKEN: "must-not-leak" },
      nowUnix: NOW_UNIX,
      platform: "win32",
      spawnSyncImpl: tools.spawnSyncImpl,
    };
    const first = ensureWindowsSourceNativeCore(options);
    assert.equal(first.reason, "downloaded");
    assert.equal(first.policy.artifactState, "source-public");
    assert.equal(tools.downloads, 1);
    validateWindowsSourceRuntimeDirectory(first.runtimeDir, PIN, { nowUnix: NOW_UNIX });

    const second = ensureWindowsSourceNativeCore({
      ...options,
      spawnSyncImpl() {
        throw new Error("verified cache must not invoke external tools");
      },
    });
    assert.equal(second.reason, "verified-cache");
    assert.equal(second.runtimeDir, first.runtimeDir);
  }));

test("Windows source cache rejects a hash-verified legacy client without fused ASR exports", () =>
  withTempRoot((root) => {
    const payloads = new Map(PAYLOADS);
    payloads.set(
      "native-core/wechatdb_client.dll",
      buildWindowsPeWithExports(["wce_client_abi_version"])
    );
    const runtimeManifest = {
      ...RUNTIME_MANIFEST,
      files: Object.fromEntries(
        [...payloads].map(([name, value]) => [
          name,
          { sha256: sha256(value), size: value.length, executable: name.endsWith(".exe") },
        ])
      ),
    };
    const runtimeManifestRaw = Buffer.from(`${JSON.stringify(runtimeManifest, null, 2)}\n`);
    const pin = { ...PIN, runtimeManifestSha256: sha256(runtimeManifestRaw) };
    writeRuntime(root, { payloads, runtimeManifestRaw });

    assert.throws(
      () => validateWindowsSourceRuntimeDirectory(root, pin, { nowUnix: NOW_UNIX }),
      /missing fused ASR ABI exports: wce_native_asr_get_status, wce_native_asr_begin, wce_native_asr_poll, wce_native_asr_close/
    );
  }));

test("Windows source bootstrap rejects an expired pin before network access", () =>
  withTempRoot((root) => {
    assert.throws(
      () => ensureWindowsSourceNativeCore({
        arch: "x64",
        cacheRoot: path.join(root, "cache"),
        configPath: writeConfig(root, { ...PIN, expiresAtUnix: NOW_UNIX }),
        env: {},
        nowUnix: NOW_UNIX,
        platform: "win32",
        spawnSyncImpl() {
          throw new Error("expired pin must fail before network access");
        },
      }),
      /已过期.*拉取最新代码/
    );
  }));

test("tracked Windows source pin selects the exact immutable public Release asset", () => {
  const trackedPin = JSON.parse(fs.readFileSync(
    path.join(__dirname, "..", "resources", "native-core-source-windows.json"),
    "utf8"
  ));
  assert.equal(
    publicReleaseUrl(trackedPin),
    "https://github.com/LifeArchiveProject/WeChatDataAnalysis/releases/download/" +
      "v2.10.0/WeChatDataAnalysis-2.10.0-Setup.exe"
  );
  assert.equal(trackedPin.assetSha256, "09a63a082503f7b1c194022ac3635885d7de737e7b422f096e1dfbc5e18a2c7d");
  assert.equal(trackedPin.buildId, "wcda-39523784335-3-windows-native");
  assert.equal(trackedPin.nativeFiles["wechatdb_client.dll"], "815b274b3fb540def1bd4bf93967f1c7a2f03d1426177af3fbf4edbf0f008299");
  assert.equal(trackedPin.nativeFiles["wechatdb_broker.exe"], "13f0db8adffb692da0d340a93def3ccf360e1729f96931d6e327dbbb33681d42");
  // 构建清单摘要与过期时间会被自动续期逻辑更新，此处仅做结构断言。
  assert.match(trackedPin.nativeFiles["wechatdb_native_build.json"], /^[0-9a-f]{64}$/);
  assert.ok(Number.isSafeInteger(trackedPin.expiresAtUnix) && trackedPin.expiresAtUnix > 0);
});

test("安装包组件缓存逐文件校验，并保持源码只读策略", () => withTempRoot(root => {
  const directory = path.join(root, "runtime");
  fs.mkdirSync(path.join(directory, "native-core"), { recursive: true });
  for (const [relative, bytes] of PAYLOADS) fs.writeFileSync(path.join(directory, relative), bytes);
  const pin = { schemaVersion: 3, platform: "win32", architecture: "x64", publisherRepository: PIN.publisherRepository,
    releaseTag: "v2.4.0", assetName: "WeChatDataAnalysis-2.4.0-Setup.exe", assetSha256: sha256(ARCHIVE_CONTENT),
    buildId: NATIVE_MANIFEST.buildId, expiresAtUnix: EXPIRES_AT_UNIX,
    nativeFiles: Object.fromEntries([...PAYLOADS].map(([name, bytes]) => [path.basename(name), sha256(bytes)])) };
  assert.equal(validateWindowsSourceRuntimeDirectory(directory, pin, { nowUnix: NOW_UNIX }).policy.artifactState, "source-public");
  const dll = path.join(directory, "native-core", "wechatdb_client.dll");
  fs.appendFileSync(dll, "modified");
  assert.throws(() => validateWindowsSourceRuntimeDirectory(directory, pin, { nowUnix: NOW_UNIX }), /摘要不匹配/);
  fs.writeFileSync(dll, PAYLOADS.get("native-core/wechatdb_client.dll"));
  const manifest = { ...NATIVE_MANIFEST, readOnlyBuild: false, wechatActions: ["write"] };
  const bytes = Buffer.from(JSON.stringify(manifest));
  fs.writeFileSync(path.join(directory, "native-core/wechatdb_native_build.json"), bytes);
  pin.nativeFiles["wechatdb_native_build.json"] = sha256(bytes);
  assert.throws(() => validateWindowsSourceRuntimeDirectory(directory, pin, { nowUnix: NOW_UNIX }), /restricted source-public/);
}));

function installerPin(overrides = {}) {
  const pin = {
    schemaVersion: 3,
    platform: "win32",
    architecture: "x64",
    publisherRepository: PIN.publisherRepository,
    releaseTag: "v2.4.0",
    assetName: "WeChatDataAnalysis-2.4.0-Setup.exe",
    assetSha256: sha256(ARCHIVE_CONTENT),
    buildId: NATIVE_MANIFEST.buildId,
    expiresAtUnix: EXPIRES_AT_UNIX,
    nativeFiles: Object.fromEntries(
      [...PAYLOADS].map(([name, bytes]) => [path.basename(name), sha256(bytes)])
    ),
  };
  return { ...pin, ...overrides };
}

function installerCacheKey(pin) {
  return `${pin.releaseTag}-${pin.assetSha256.slice(0, 12)}`;
}

function writeInstallerRuntime(directory) {
  fs.mkdirSync(path.join(directory, "native-core"), { recursive: true });
  for (const [relative, bytes] of PAYLOADS) {
    fs.writeFileSync(path.join(directory, relative), bytes);
  }
}

function installerTools(pin, { mirror = "" } = {}) {
  let downloads = 0;
  const expectedUrl = mirror
    ? `${mirror.replace(/\/+$/, "")}/${publicReleaseUrl(pin)}`
    : publicReleaseUrl(pin);
  const spawnSyncImpl = (command, args, options) => {
    if (command === "curl.exe") {
      downloads += 1;
      assert.equal(options.shell, false);
      assert.equal(args.at(-1), expectedUrl);
      fs.writeFileSync(args[args.indexOf("--output") + 1], ARCHIVE_CONTENT);
      return { status: 0, stdout: "", stderr: "" };
    }
    if (command.endsWith("7z.exe") && args.includes("$PLUGINSDIR/app-64.7z")) {
      const output = args.find((arg) => arg.startsWith("-o")).slice(2);
      fs.writeFileSync(path.join(output, "app-64.7z"), ARCHIVE_CONTENT);
      return { status: 0, stdout: "", stderr: "" };
    }
    if (command.endsWith("7z.exe")) {
      const output = args.find((arg) => arg.startsWith("-o")).slice(2);
      fs.mkdirSync(output, { recursive: true });
      for (const [relative, bytes] of PAYLOADS) {
        fs.writeFileSync(path.join(output, path.basename(relative)), bytes);
      }
      return { status: 0, stdout: "", stderr: "" };
    }
    throw new Error(`unexpected command: ${command} ${args.join(" ")}`);
  };
  return { spawnSyncImpl, get downloads() { return downloads; } };
}

test("过期安装包 pin 从完好缓存自动续期，无需网络", () =>
  withTempRoot((root) => {
    const pin = installerPin({ expiresAtUnix: NOW_UNIX });
    const configPath = writeConfig(root, pin);
    writeInstallerRuntime(path.join(root, "cache", installerCacheKey(pin)));
    const options = {
      arch: "x64",
      cacheRoot: path.join(root, "cache"),
      configPath,
      env: {},
      nowUnix: NOW_UNIX,
      platform: "win32",
      spawnSyncImpl() {
        throw new Error("完好缓存续期不得访问外部工具");
      },
    };
    const first = ensureWindowsSourceNativeCore(options);
    assert.equal(first.reason, "renewed-cache");
    const renewedPin = JSON.parse(fs.readFileSync(configPath, "utf8"));
    assert.equal(renewedPin.expiresAtUnix, NOW_UNIX + 45 * 24 * 60 * 60);
    assert.equal(renewedPin.buildId, pin.buildId);
    assert.equal(
      renewedPin.nativeFiles["wechatdb_client.dll"],
      pin.nativeFiles["wechatdb_client.dll"]
    );
    assert.match(renewedPin.nativeFiles["wechatdb_native_build.json"], /^[0-9a-f]{64}$/);
    assert.notEqual(
      renewedPin.nativeFiles["wechatdb_native_build.json"],
      pin.nativeFiles["wechatdb_native_build.json"]
    );
    validateWindowsSourceRuntimeDirectory(
      path.join(root, "cache", installerCacheKey(pin)),
      renewedPin,
      { nowUnix: NOW_UNIX }
    );
    const second = ensureWindowsSourceNativeCore(options);
    assert.equal(second.reason, "verified-cache");
  }));

test("过期安装包 pin 缓存缺失时经镜像下载后自动续期", () =>
  withTempRoot((root) => {
    const mirror = "https://mirror.example/";
    const pin = installerPin({ expiresAtUnix: NOW_UNIX });
    const configPath = writeConfig(root, pin);
    const tools = installerTools(pin, { mirror });
    const options = {
      arch: "x64",
      cacheRoot: path.join(root, "cache"),
      configPath,
      env: { WCE_GITHUB_MIRROR: mirror },
      nowUnix: NOW_UNIX,
      platform: "win32",
      spawnSyncImpl: tools.spawnSyncImpl,
    };
    const first = ensureWindowsSourceNativeCore(options);
    assert.equal(first.reason, "renewed-downloaded");
    assert.equal(tools.downloads, 1);
    assert.equal(fs.readdirSync(path.join(root, "cache")).length, 1);
    const second = ensureWindowsSourceNativeCore({
      ...options,
      spawnSyncImpl() {
        throw new Error("verified cache must not invoke external tools");
      },
    });
    assert.equal(second.reason, "verified-cache");
  }));

test("generic source bootstrap wires the verified Windows directory without macOS-only variables", () =>
  withTempRoot((root) => {
    const tools = fixtureTools();
    const result = ensureSourceNativeCore({
      arch: "x64",
      cacheRoot: path.join(root, "cache"),
      configPath: writeConfig(root),
      env: {},
      nowUnix: NOW_UNIX,
      platform: "win32",
      spawnSyncImpl: tools.spawnSyncImpl,
    });
    const env = {};
    applySourceRuntimeEnvironment(env, result);
    assert.equal(env.WCE_NATIVE_CORE_SOURCE_DIR, result.nativeDir);
    assert.equal("WECHAT_TOOL_MACOS_DB_KEY_BUNDLE" in env, false);
    assert.equal("WCE_INTEGRITY_NATIVE_PATH" in env, false);
  }));

test("direct Electron source launch performs the Windows bootstrap before runtime policy", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  const windowsGate = source.indexOf('process.platform === "win32"');
  const bootstrap = source.indexOf("ensureSourceNativeCore({ env })", windowsGate);
  const policy = source.indexOf("applyNativeCoreRuntimePolicy(env", bootstrap);
  assert.ok(windowsGate > 0);
  assert.ok(bootstrap > windowsGate);
  assert.ok(policy > bootstrap);
});
