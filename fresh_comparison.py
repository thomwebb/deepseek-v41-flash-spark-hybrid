"""Fresh same-host three-stack throughput comparison; always restore hybrid."""
import hashlib
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time

from runtime import ROOT, WORKER, health, remote, run, write_json

JAY = Path.home() / 'spark-ab-20261031'
ORIGINAL = 'dsv41-1017-exl3-tf'
CANDIDATE = 'spark-ab-jay-20261031'
STAGE = ROOT / 'results' / ('fresh-comparison-' + time.strftime('%Y%m%d-%H%M%S'))


def stop_jay():
    run(['docker', 'stop', '-t', '25', CANDIDATE + '-r0'], 45, False)
    remote('docker stop -t 25 ' + CANDIDATE + '-r1', 45, False)
    run(['bash', Path.home() / 'DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks/scripts/memguard.sh',
         'stop', STAGE / 'jay-head-guard.pid'], check=False)
    remote(f'bash ~/.cache/dsv41-tensorfold/memguard.sh stop {STAGE}/jay-worker-guard.pid', check=False)


def restore():
    stop_jay()
    # Start, but do not change boot policy: which supervisor runs at boot is
    # owned by install-supervisor.sh and promote_grammar.py, not by a benchmark.
    run(['systemctl', '--user', 'stop', 'dsv41.service'], 180, False)
    run(['systemctl', '--user', 'start', 'dsv41-hybrid.service'], 180)
    wait_healthy('spark-hybrid-grammar-r0')


def wait_healthy(name, seconds=900):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = run(['docker', 'inspect', '-f', '{{.State.Running}}', name], check=False)
        if state.returncode == 0 and state.stdout.strip() == 'true':
            try:
                h = health()
                if h.get('status') == 'ok' and not h.get('warming'):
                    return
            except (OSError, ValueError):
                pass
        time.sleep(4)
    raise RuntimeError('Readiness deadline: ' + name)


def identity(name):
    # Never dump full inspect data or environment, which can contain credentials.
    text = run(['docker', 'inspect', '--format',
                '{{.Image}}|{{.Config.Image}}|{{.Id}}', name]).stdout.strip()
    return text


def benchmark(stack, count=2):
    if health().get('live', {}).get('connections', 0):
        raise RuntimeError('External traffic detected before ' + stack)
    records = []
    for index in range(count + 1):
        label = 'warmup' if index == 0 else 'run-' + str(index)
        output = STAGE / (stack + '-' + label + '.json')
        result = run([sys.executable, ROOT / 'ab_benchmark.py', 'http://127.0.0.1:8888', output],
                     310, False, STAGE / (stack + '-' + label + '.log'))
        r = json.loads(output.read_text())
        if not r.get('health_after', {}).get('ok') or r.get('refused_busy_server'):
            raise RuntimeError('Unhealthy/busy benchmark ' + stack)
        # Original recipe lacks schema support: preserve and report its failed test.
        metrics = {kind: r['summary'][kind]['median_decode_tokens_per_second_approx']
                   for kind in ('code', 'prose')}
        metrics['aggregate'] = r['mixed_batch']['completion_tokens_per_second']
        metrics.update({'file': output.name, 'returncode': result.returncode,
                        'passed': sum(bool(x.get('ok')) for x in r['results']),
                        'total': len(r['results']),
                        'failures': [x['label'] for x in r['results'] if not x.get('ok')]})
        records.append(metrics)
        print('BENCH', stack, label, json.dumps(metrics), flush=True)
    return {'warmup': records[0], 'runs': records[1:],
            'median': {k: statistics.median(r[k] for r in records[1:])
                       for k in ('code', 'prose', 'aggregate')}}


def main():
    os.umask(0o077)
    STAGE.mkdir()
    report = {'started': time.time(), 'stage': str(STAGE), 'stacks': {},
              'method': 'One discarded full warmup plus two measured runs per stack, same benchmark file, prompts and seeds; final hybrid repeat for drift.',
              'benchmark_sha256': hashlib.sha256((ROOT / 'ab_benchmark.py').read_bytes()).hexdigest()}
    write_json(STAGE / 'report.json', report)
    # Independent, bounded fallback if controller disappears. Do not disable hybrid boot policy.
    fallback = (f'sleep 2400; if [ ! -f "{STAGE}/complete" ]; then '
                f'docker stop -t 25 {CANDIDATE}-r0; '
                f'ssh -o BatchMode=yes -o ConnectTimeout=10 {WORKER} "docker stop -t 25 {CANDIDATE}-r1"; '
                'systemctl --user stop dsv41.service; '
                'systemctl --user start dsv41-hybrid.service; fi')
    guardian = subprocess.Popen(['sh', '-c', fallback], start_new_session=True,
                                stdout=(STAGE / 'guardian.log').open('w'), stderr=subprocess.STDOUT)
    try:
        run(['sha256sum', '-c', ROOT / 'rollback/config-sha256.txt'])
        run([sys.executable, ROOT / 'verify_grammar.py'])
        report['hybrid_identity'] = identity('spark-hybrid-grammar-r0')
        report['stacks']['hybrid'] = benchmark('hybrid')
        write_json(STAGE / 'report.json', report)
        # Disable competing restart loop before bringing up the original unit.
        run(['systemctl', '--user', 'stop', 'dsv41-hybrid.service'], 180)
        run(['systemctl', '--user', 'start', 'dsv41.service'], 180)
        wait_healthy(ORIGINAL)
        report['mia_identity'] = identity(ORIGINAL)
        report['stacks']['mia'] = benchmark('mia')
        write_json(STAGE / 'report.json', report)
        run(['systemctl', '--user', 'stop', 'dsv41.service'], 180)
        # Use the exact previously staged Jay recipe, image, weights and configuration.
        remote(f'cd {JAY}/recipe && CONFIG=config/prod-zig.env bash scripts/run-zig.sh 1 --detach', 180)
        run(['bash', '-c', f'cd {JAY}/recipe && CONFIG=config/prod-zig.env bash scripts/run-zig.sh 0 --detach'], 180)
        remote(f'mkdir -p {STAGE}')
        run(['bash', Path.home() / 'DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks/scripts/memguard.sh',
             'start', CANDIDATE + '-r0', '3.5', STAGE / 'jay-head-memguard.log',
             STAGE / 'jay-head-guard.pid', '2', '2.5'])
        remote(f'bash ~/.cache/dsv41-tensorfold/memguard.sh start {CANDIDATE}-r1 3.5 '
               f'{STAGE}/jay-worker-memguard.log {STAGE}/jay-worker-guard.pid 2 2.5')
        wait_healthy(CANDIDATE + '-r0')
        report['jay_identity'] = identity(CANDIDATE + '-r0')
        report['stacks']['jay'] = benchmark('jay')
        write_json(STAGE / 'report.json', report)
    except Exception as error:
        report['error'] = str(error)
        print('COMPARISON_ERROR', str(error), flush=True)
    finally:
        try:
            restore()
            report['hybrid_restored'] = True
            report['hybrid_final_identity'] = identity('spark-hybrid-grammar-r0')
            if 'error' not in report:
                report['stacks']['hybrid_final'] = benchmark('hybrid-final', count=1)
            run(['sha256sum', '-c', ROOT / 'rollback/config-sha256.txt'])
            (STAGE / 'complete').write_text('HYBRID_RESTORED\n')
            os.killpg(guardian.pid, signal.SIGTERM)
        except Exception as error:
            report['restore_error'] = str(error)
            print('RESTORE_ERROR', str(error), flush=True)
        report['finished'] = time.time()
        write_json(STAGE / 'report.json', report)
        write_json(ROOT / 'results/fresh-comparison-latest.json', report)
        print('FINAL', json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
