<?php
/**
 * ym-helper.php - server-side helper for yourls-manager.
 *
 * Uploaded by `ym deploy` to ~/.yourls-manager/ym-helper-<version>.php (outside the web root)
 * and run with the PHP CLI. Never place this file in a web-accessible directory.
 *
 * Usage:
 *   php ym-helper.php info    --root=WEBROOT
 *   php ym-helper.php dump    --config=CONFIG.php --out=FILE.sql.gz [--mysqldump=PATH]
 *   php ym-helper.php upgrade --root=WEBROOT
 *   php ym-helper.php plugins --root=WEBROOT --active=a,b,c
 *   php ym-helper.php version
 *
 * Output: exactly one JSON object on stdout ({"ok": true, ...} or {"ok": false, "error": ...});
 * human-readable logs on stderr. Exit status 0 on success, 1 on error. The YOURLS config is
 * found via YOURLS_CONFIG_FILE (ym sets it to the target's config_file).
 *
 * Results:
 *   version  helper_version, php. Does not load YOURLS.
 *   info     php, yourls_version, db_version_code, db_version_installed, installed,
 *            upgrade_needed, active_plugins. Read-only (bootstraps with YOURLS_INSTALLING).
 *   dump     out, bytes, tables; or skipped + reason for a database without tables. Reads
 *            only the config file; mysqldump --single-transaction, gzipped, mode 0600.
 *   upgrade  upgraded (false when not needed), from, to, from_version, to_version. Refuses
 *            when YOURLS isn't installed and for 1.3 databases (db_version 100).
 *   plugins  activated, deactivated, active: the active set becomes exactly --active.
 *
 * https://github.com/RubenFixit/yourls-manager - MIT License
 */

if (PHP_SAPI !== 'cli') {
    if (!headers_sent()) {
        http_response_code(404);
    }
    exit(1);
}

const YM_HELPER_VERSION = '0.9.1'; // = yourls-manager's version (tests/test_version.py)
const YM_PLUGIN_NAME_RE = '/^[A-Za-z0-9][A-Za-z0-9._-]*$/';

$GLOBALS['ym_finished'] = false;
$GLOBALS['ym_command'] = '';
$GLOBALS['ym_phase'] = 'start';

function ym_log(string $message): void
{
    fwrite(STDERR, '[ym-helper] ' . $message . "\n");
}

/** Drain any output buffers (YOURLS chatter) to stderr so stdout stays pure JSON. */
function ym_drain_buffers(): string
{
    $captured = '';
    while (ob_get_level() > 0) {
        $captured = ob_get_clean() . $captured;
    }
    $text = trim(html_entity_decode(strip_tags($captured), ENT_QUOTES | ENT_HTML5));
    $text = preg_replace("/\n\s*\n+/", "\n", $text);
    if ($text !== '') {
        foreach (explode("\n", $text) as $line) {
            ym_log('yourls: ' . trim($line));
        }
    }
    return $text;
}

function ym_emit(array $payload, int $code): void
{
    $GLOBALS['ym_finished'] = true;
    ym_drain_buffers();
    fwrite(STDOUT, json_encode($payload, JSON_UNESCAPED_SLASHES | JSON_PARTIAL_OUTPUT_ON_ERROR) . "\n");
    exit($code);
}

function ym_ok(array $data = []): void
{
    ym_emit(['ok' => true, 'command' => $GLOBALS['ym_command']] + $data, 0);
}

function ym_fail(string $error, array $extra = []): void
{
    ym_emit(['ok' => false, 'command' => $GLOBALS['ym_command'], 'error' => $error] + $extra, 1);
}

/** Catches YOURLS calling exit()/die() during bootstrap (install redirect, maintenance, DB). */
function ym_shutdown_guard(): void
{
    if ($GLOBALS['ym_finished']) {
        return;
    }
    $output = ym_drain_buffers();
    $last = error_get_last();
    $hint = '';
    if (preg_match('/(Incorrect DB config|could not connect to DB)[^\n]*/i', $output, $m)) {
        $hint = 'YOURLS could not connect to the database: ' . trim($m[0]);
    } elseif (function_exists('yourls_is_installed') && function_exists('yourls_get_db')) {
        try {
            if (!yourls_is_installed()) {
                $hint = 'YOURLS is not installed in this database; run the installer via the browser first (admin/install.php)';
            }
        } catch (Throwable $e) {
            // ignore
        }
    }
    $error = $hint !== '' ? $hint : 'helper exited prematurely during ' . $GLOBALS['ym_phase'];
    $extra = [];
    if ($output !== '') {
        $extra['output'] = substr($output, 0, 4000);
    }
    if ($last !== null && in_array($last['type'], [E_ERROR, E_PARSE, E_CORE_ERROR, E_COMPILE_ERROR, E_USER_ERROR], true)) {
        $extra['php_error'] = $last['message'] . ' (' . $last['file'] . ':' . $last['line'] . ')';
    }
    $GLOBALS['ym_finished'] = true;
    fwrite(STDOUT, json_encode(['ok' => false, 'command' => $GLOBALS['ym_command'], 'error' => $error] + $extra, JSON_UNESCAPED_SLASHES | JSON_PARTIAL_OUTPUT_ON_ERROR) . "\n");
    exit(1);
}

function ym_parse_options(array $args): array
{
    $opts = [];
    foreach ($args as $arg) {
        if (!preg_match('/^--([a-z][a-z0-9-]*)(?:=(.*))?$/s', $arg, $m)) {
            ym_fail("unexpected argument: $arg");
        }
        $opts[$m[1]] = array_key_exists(2, $m) ? $m[2] : true;
    }
    return $opts;
}

function ym_require_option(array $opts, string $name): string
{
    if (!isset($opts[$name]) || !is_string($opts[$name]) || $opts[$name] === '') {
        ym_fail("missing required option --$name=...");
    }
    return $opts[$name];
}

/** Absolute path of a YOURLS root, validated. */
function ym_resolve_root(string $root): string
{
    $real = realpath($root);
    if ($real === false || !is_file($real . '/includes/load-yourls.php')) {
        ym_fail("not a YOURLS root (includes/load-yourls.php missing): $root");
    }
    return $real;
}

function ym_find_config(string $root): string
{
    foreach ([$root . '/user/config.php', $root . '/includes/config.php'] as $candidate) {
        if (is_file($candidate)) {
            return $candidate;
        }
    }
    ym_fail("no user/config.php in $root");
    return '';
}

function ym_fake_request_env(): void
{
    $site = defined('YOURLS_SITE') ? (string) YOURLS_SITE : 'http://localhost';
    $parts = parse_url($site) ?: [];
    $host = ($parts['host'] ?? 'localhost') . (isset($parts['port']) ? ':' . $parts['port'] : '');
    $_SERVER['HTTP_HOST'] = $host;
    $_SERVER['SERVER_NAME'] = $parts['host'] ?? 'localhost';
    $_SERVER['REQUEST_URI'] = '/';
    $_SERVER['REQUEST_METHOD'] = 'GET';
    $_SERVER['REMOTE_ADDR'] = '127.0.0.1';
    if (($parts['scheme'] ?? '') === 'https') {
        $_SERVER['HTTPS'] = 'on';
    }
}

function ym_active_plugin_ids(): array
{
    $active = yourls_get_option('active_plugins');
    if (!is_array($active)) {
        $active = [];
    }
    return array_values(array_filter($active, 'is_string'));
}

function ym_names(array $ids): array
{
    return array_values(array_map(static fn($id) => dirname($id), $ids));
}

/** Plugins are not loaded while YOURLS_UPGRADING; seed YDB's active list from the option. */
function ym_seed_active_plugins(): void
{
    yourls_get_db('read-ym-helper')->set_plugins(ym_active_plugin_ids());
}

// ------------------------------------------------------------------------------- commands

function ym_cmd_info(): void
{
    $installed = (bool) yourls_is_installed();
    $db_installed = null;
    $active = [];
    if ($installed) {
        [, $sql] = yourls_get_current_version_from_sql();
        $db_installed = (int) $sql;
        $active = ym_names(ym_active_plugin_ids());
    }
    $code = (int) YOURLS_DB_VERSION;
    ym_ok([
        'php' => PHP_VERSION,
        'yourls_version' => YOURLS_VERSION,
        'db_version_code' => $code,
        'db_version_installed' => $db_installed,
        'installed' => $installed,
        'upgrade_needed' => $installed && $db_installed < $code,
        'active_plugins' => $active,
    ]);
}

function ym_cmd_upgrade(): void
{
    if (!yourls_is_installed()) {
        ym_fail('YOURLS is not installed in this database; run install via browser first (admin/install.php)');
    }
    [$oldver, $oldsql] = yourls_get_current_version_from_sql();
    $oldsql = (int) $oldsql;
    if (!yourls_upgrade_is_needed()) {
        ym_ok(['upgraded' => false, 'db_version' => $oldsql, 'yourls_version' => YOURLS_VERSION]);
    }
    if ($oldsql === 100) {
        ym_fail('database is from YOURLS 1.3 (db_version 100); that multi-step upgrade must be run via the browser (admin/upgrade.php)');
    }
    $newver = YOURLS_VERSION;
    $newsql = (int) YOURLS_DB_VERSION;
    ym_log("upgrading database: $oldver (db $oldsql) -> $newver (db $newsql)");
    $GLOBALS['ym_phase'] = 'yourls_upgrade';
    ob_start();
    yourls_upgrade(1, $oldver, $newver, $oldsql, $newsql);
    yourls_upgrade(3, $oldver, $newver, $oldsql, $newsql);
    $log = trim(strip_tags((string) ob_get_clean()));
    if ($log !== '') {
        foreach (explode("\n", $log) as $line) {
            if (trim($line) !== '') {
                ym_log('upgrade: ' . trim($line));
            }
        }
    }
    if (yourls_upgrade_is_needed()) {
        ym_fail('upgrade ran but the database still reports an upgrade is needed');
    }
    ym_ok([
        'upgraded' => true,
        'from' => $oldsql,
        'to' => $newsql,
        'from_version' => (string) $oldver,
        'to_version' => $newver,
    ]);
}

function ym_cmd_plugins(array $opts): void
{
    if (!yourls_is_installed()) {
        ym_fail('YOURLS is not installed in this database; run install via browser first (admin/install.php)');
    }
    if (!array_key_exists('active', $opts) || $opts['active'] === true) {
        ym_fail('missing required option --active=a,b,c (may be empty: --active=)');
    }
    $wanted = [];
    foreach (explode(',', (string) $opts['active']) as $name) {
        $name = trim($name);
        if ($name === '') {
            continue;
        }
        if (!preg_match(YM_PLUGIN_NAME_RE, $name)) {
            ym_fail("invalid plugin name: $name");
        }
        $wanted[$name] = $name . '/plugin.php';
    }
    $missing = [];
    foreach ($wanted as $name => $id) {
        if (!is_file(YOURLS_PLUGINDIR . '/' . $id)) {
            $missing[] = $name;
        }
    }
    if ($missing) {
        ym_fail('plugin file(s) missing: ' . implode(', ', array_map(static fn($n) => "user/plugins/$n/plugin.php", $missing)), ['missing' => $missing]);
    }

    ym_seed_active_plugins();
    $before = ym_active_plugin_ids();
    $activated = [];
    $deactivated = [];
    $errors = [];

    $GLOBALS['ym_phase'] = 'plugin deactivation';
    foreach ($before as $id) {
        if (in_array($id, $wanted, true)) {
            continue;
        }
        $result = yourls_deactivate_plugin($id);
        if ($result === true) {
            $deactivated[] = dirname($id);
            ym_log('deactivated ' . dirname($id));
        } else {
            $errors[] = dirname($id) . ': ' . trim(strip_tags((string) $result));
        }
    }
    $GLOBALS['ym_phase'] = 'plugin activation';
    foreach ($wanted as $name => $id) {
        if (in_array($id, ym_active_plugin_ids(), true)) {
            continue;
        }
        $result = yourls_activate_plugin(YOURLS_PLUGINDIR . '/' . $id);
        if ($result === true) {
            $activated[] = $name;
            ym_log("activated $name");
        } else {
            $errors[] = $name . ': ' . trim(strip_tags((string) $result));
        }
    }
    $result = [
        'activated' => $activated,
        'deactivated' => $deactivated,
        'active' => ym_names(ym_active_plugin_ids()),
    ];
    if ($errors) {
        ym_fail('some plugins could not be (de)activated: ' . implode('; ', $errors), $result + ['errors' => $errors]);
    }
    ym_ok($result);
}

/** Parse YOURLS_DB_HOST: "host", "host:port", "host:/path/to/socket", "/path/to/socket". */
function ym_parse_db_host(string $value): array
{
    $value = trim($value);
    if ($value !== '' && $value[0] === '/') {
        return ['host' => 'localhost', 'port' => null, 'socket' => $value];
    }
    if (preg_match('/^\[([0-9a-fA-F:.]+)\](?::(\d+))?$/', $value, $m)) {
        return ['host' => $m[1], 'port' => isset($m[2]) ? (int) $m[2] : null, 'socket' => null];
    }
    $pos = strrpos($value, ':');
    if ($pos !== false && substr_count($value, ':') === 1) {
        $host = substr($value, 0, $pos);
        $rest = substr($value, $pos + 1);
        if ($rest !== '' && $rest[0] === '/') {
            return ['host' => $host !== '' ? $host : 'localhost', 'port' => null, 'socket' => $rest];
        }
        if (ctype_digit($rest)) {
            return ['host' => $host, 'port' => (int) $rest, 'socket' => null];
        }
    }
    return ['host' => $value !== '' ? $value : 'localhost', 'port' => null, 'socket' => null];
}

function ym_option_file_value(string $value): string
{
    return '"' . str_replace(['\\', '"', "\n", "\r", "\t"], ['\\\\', '\\"', '\\n', '\\r', '\\t'], $value) . '"';
}

function ym_db_table_count(array $db): ?int
{
    if (!extension_loaded('pdo_mysql')) {
        return null;
    }
    $dsn = 'mysql:dbname=' . $db['name'] . ';charset=utf8mb4';
    $dsn .= $db['socket'] !== null ? ';unix_socket=' . $db['socket'] : ';host=' . $db['host'] . ($db['port'] ? ';port=' . $db['port'] : '');
    try {
        $pdo = new PDO($dsn, $db['user'], $db['pass'], [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_TIMEOUT => 10]);
        return count($pdo->query('SHOW TABLES')->fetchAll(PDO::FETCH_COLUMN));
    } catch (Throwable $e) {
        ym_fail('cannot connect to the database: ' . $e->getMessage());
    }
    return null;
}

function ym_cmd_dump(array $opts): void
{
    $config = ym_require_option($opts, 'config');
    $out = ym_require_option($opts, 'out');
    $mysqldump = is_string($opts['mysqldump'] ?? null) ? $opts['mysqldump'] : 'mysqldump';
    if (!is_readable($config)) {
        ym_fail("config file not readable: $config");
    }
    $GLOBALS['ym_phase'] = 'reading config';
    ob_start();
    (static function (string $file): void {
        require $file;
    })($config);
    ym_drain_buffers();
    foreach (['YOURLS_DB_USER', 'YOURLS_DB_PASS', 'YOURLS_DB_NAME', 'YOURLS_DB_HOST'] as $const) {
        if (!defined($const)) {
            ym_fail("$const is not defined by $config");
        }
    }
    $db = ym_parse_db_host((string) YOURLS_DB_HOST) + [
        'user' => (string) YOURLS_DB_USER,
        'pass' => (string) YOURLS_DB_PASS,
        'name' => (string) YOURLS_DB_NAME,
    ];

    $GLOBALS['ym_phase'] = 'dump';
    $tables = ym_db_table_count($db);
    if ($tables === 0) {
        ym_ok(['skipped' => true, 'reason' => 'database has no tables (first deploy?)']);
    }

    $dir = dirname($out);
    if (!is_dir($dir) && !mkdir($dir, 0700, true) && !is_dir($dir)) {
        ym_fail("cannot create backup directory $dir");
    }

    $defaults = tempnam(sys_get_temp_dir(), 'ym-my-');
    if ($defaults === false) {
        ym_fail('cannot create temporary defaults file');
    }
    chmod($defaults, 0600);
    $ini = "[client]\n" . 'user=' . ym_option_file_value($db['user']) . "\n"
        . 'password=' . ym_option_file_value($db['pass']) . "\n";
    if ($db['socket'] !== null) {
        $ini .= 'socket=' . ym_option_file_value($db['socket']) . "\n";
    } else {
        $ini .= 'host=' . ym_option_file_value($db['host']) . "\n";
        if ($db['port']) {
            $ini .= 'port=' . (int) $db['port'] . "\n";
        }
    }
    file_put_contents($defaults, $ini);

    $tmp_out = $out . '.part';
    $cmd = [
        $mysqldump,
        '--defaults-extra-file=' . $defaults,
        '--single-transaction',
        '--no-tablespaces',
        '--default-character-set=utf8mb4',
        '--',
        $db['name'],
    ];
    $cmdline = implode(' ', array_map('escapeshellarg', $cmd));
    $shell = $cmdline . ' | gzip -c > ' . escapeshellarg($tmp_out) . '; echo "${PIPESTATUS[0]:-x} ${PIPESTATUS[1]:-x}" >&3';
    // Use bash for PIPESTATUS when available; fall back to sh with set -o pipefail semantics.
    $bash = is_executable('/bin/bash') ? '/bin/bash' : null;
    $old_umask = umask(0077);
    if ($bash !== null) {
        $proc = proc_open([$bash, '-c', $shell], [1 => ['pipe', 'w'], 2 => ['pipe', 'w'], 3 => ['pipe', 'w']], $pipes);
    } else {
        $shell = $cmdline . ' | gzip -c > ' . escapeshellarg($tmp_out);
        $proc = proc_open(['/bin/sh', '-c', $shell], [1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    }
    if (!is_resource($proc)) {
        umask($old_umask);
        @unlink($defaults);
        ym_fail('could not start mysqldump');
    }
    $stdout = stream_get_contents($pipes[1]);
    $stderr = stream_get_contents($pipes[2]);
    $status = isset($pipes[3]) ? trim((string) stream_get_contents($pipes[3])) : '';
    foreach ($pipes as $p) {
        fclose($p);
    }
    $exit = proc_close($proc);
    umask($old_umask);
    @unlink($defaults);
    $dump_ok = $exit === 0 && ($status === '' || $status === '0 0');
    $size = is_file($tmp_out) ? filesize($tmp_out) : 0;
    if (!$dump_ok || $size < 20) {
        @unlink($tmp_out);
        ym_fail('mysqldump failed: ' . trim($stderr . ' ' . $stdout) . ($status !== '' ? " (pipe status $status)" : ''));
    }
    if (trim($stderr) !== '') {
        ym_log('mysqldump: ' . trim($stderr));
    }
    if (!rename($tmp_out, $out)) {
        @unlink($tmp_out);
        ym_fail("cannot move dump into place: $out");
    }
    chmod($out, 0600);
    ym_ok(['out' => $out, 'bytes' => filesize($out), 'tables' => $tables]);
}

// ------------------------------------------------------------------------------- main

register_shutdown_function('ym_shutdown_guard');
set_exception_handler(static function (Throwable $e): void {
    ym_fail(get_class($e) . ': ' . $e->getMessage(), ['where' => $e->getFile() . ':' . $e->getLine()]);
});

$ym_args = array_slice($argv ?? [], 1);
$GLOBALS['ym_command'] = (string) ($ym_args[0] ?? '');
$ym_opts = ym_parse_options(array_slice($ym_args, 1));

switch ($GLOBALS['ym_command']) {
    case 'version':
        ym_ok(['helper_version' => YM_HELPER_VERSION, 'php' => PHP_VERSION]);
        break;
    case 'dump':
        ym_cmd_dump($ym_opts);
        break;
    case 'info':
    case 'upgrade':
    case 'plugins':
        break;
    case '':
        ym_fail('usage: ym-helper.php info|dump|upgrade|plugins|version [--options]');
        break;
    default:
        ym_fail('unknown command: ' . $GLOBALS['ym_command']);
}

// Commands below bootstrap YOURLS. This must happen at global scope: YOURLS' config file
// defines globals such as $yourls_user_passwords and $yourls_reserved_URL.
$ym_root = ym_resolve_root(ym_require_option($ym_opts, 'root'));
if ($GLOBALS['ym_command'] === 'info') {
    // Read-only: bypass the install redirect and the upgrade redirect / plugin loading.
    define('YOURLS_INSTALLING', true);
}
define('YOURLS_UPGRADING', true);
define('YOURLS_CONFIGFILE', ym_find_config($ym_root));

$GLOBALS['ym_phase'] = 'loading config';
ob_start();
require_once YOURLS_CONFIGFILE;
ym_fake_request_env();
$GLOBALS['ym_phase'] = 'YOURLS bootstrap';
require_once $ym_root . '/includes/load-yourls.php';
$GLOBALS['ym_phase'] = $GLOBALS['ym_command'];

switch ($GLOBALS['ym_command']) {
    case 'info':
        ym_cmd_info();
        break;
    case 'upgrade':
        ym_cmd_upgrade();
        break;
    case 'plugins':
        ym_cmd_plugins($ym_opts);
        break;
}
ym_fail('internal error: command fell through');
