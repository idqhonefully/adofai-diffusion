# 一次性驱动：把指定歌曲跑 large 混合推理流水线，结果拷到桌面。
# 流程：ffmpeg 转 WAV -> BS-RoFormer 分 6 轨 -> 逐轨 MuScriptor(large 混合) -> 合并 MIDI
import sys, os, time, shutil, traceback
sys.path.insert(0, r'<REPO>/gui')
import backend  # noqa: E402

AUDIO = r'<PICT>/HyuN - Grin.mp3'
DESKTOP = r'<PICT>'
SIZE = 'large'
LOG = r'<REPO>/audio-sep/bench/grin_run.log'

lf = open(LOG, 'w', encoding='utf-8', buffering=1)

def log(s):
    lf.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), s))
    lf.flush()

# 默认即：large 走混合推理 / auto 层数 / fp32（零质量损失）
os.environ.setdefault('ADOFAI_MUS_HYBRID', '1')
os.environ.setdefault('ADOFAI_MUS_GPU_LAYERS', 'auto')
os.environ.setdefault('ADOFAI_MUS_GPU_DTYPE', 'fp32')

log('start: %s  size=%s' % (AUDIO, SIZE))
log('gpu baseline: ' + (lambda: __import__('subprocess').check_output(
    ['nvidia-smi', '--query-gpu=memory.used,utilization.gpu', '--format=csv,noheader'],
    text=True).strip())())

t0 = time.time()
try:
    info = backend.to_midi(
        AUDIO, size=SIZE,
        on_log=log,
        on_stage=lambda n, name, det: log('STAGE %d: %s (%s)' % (n, name, det)),
    )
    elapsed = time.time() - t0
    log('=== DONE in %.1f min (%.0fs) ===' % (elapsed / 60, elapsed))
    log('combined midi: %s' % info['midi'])
    log('stems midi: %s' % info['stems'])
    if info.get('failed'):
        log('FAILED stems: %s' % info['failed'])

    base = 'HyuN - Grin'
    dst_combined = os.path.join(DESKTOP, base + '_stems_combined.mid')
    shutil.copy2(info['midi'], dst_combined)
    log('copied combined -> %s' % dst_combined)

    stem_dir = os.path.join(DESKTOP, base + ' - stems')
    os.makedirs(stem_dir, exist_ok=True)
    for _k, p in info['stems'].items():
        shutil.copy2(p, os.path.join(stem_dir, os.path.basename(p)))
    log('copied %d stem mids -> %s' % (len(info['stems']), stem_dir))
    log('SUCCESS')
except Exception as e:  # noqa: BLE001
    log('ERROR: %r' % e)
    traceback.print_exc(file=lf)
    log('FAILED')
finally:
    lf.close()
