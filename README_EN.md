# ADOFAI Maker 128

An open-source project that **turns music into an ADOFAI (A Dance of Fire and Ice) chart automatically**.

In plain terms: you drop in a song, it first listens for the beats/onsets (the "footsteps"), then choreographs spins and patterns on top, and finally spits out a playable `.adofai` chart.

> This is the **128 full-chain** version: onset-detection and chart generation share the same time grid (hop = 128, ~5.8 ms/frame), so the footsteps lock precisely onto the music with zero timing drift. (Piano pieces perform worse because of limited training data.)

---

## What it does

- **Automatic beat/onset detection**: a neural network (**OnsetNet**) finds where you should tap, far more accurate than the old-school "onset/peak detector".
- **Automatic pattern generation**: a **VAE + diffusion** model generates spins (Twirl) and step patterns on top of the detected beats.
- **Stem separation + multi-select**: uses **Demucs** to split a song into drums / bass / melody / vocals / accompaniment. You can let the AI focus on a single stem, or check multiple stems and merge them before feeding to the model.
- **Web UI**: upload audio → separate stems → pick stems → one-click generate, with a mel-spectrogram preview; the generated chart is downloadable. Prefer the original audio, since separated stems have lower quality and can cause false onsets.
- **Trainable**: ships with training scripts so you can train the models on your own chart data (especially for the genres you actually make).

> Note: the current version does **not** generate camera moves / color changes / flash effects — it only does "footwork" (beats + steps).

---

## Quick start

### Option 1: Offline all-in-one bundle (easiest, recommended)

Don't want to install Python / CUDA / train models yourself? There is a **ready-to-run offline bundle** — a single self-contained package (~3.3 GB compressed) that already bundles Python, CUDA-enabled PyTorch, the Demucs weights, and the three trained model weights. Just unzip and double-click `run.bat`.

- **Requires**: Windows + an NVIDIA discrete GPU (the bundle ships CUDA PyTorch; there is no CPU fallback).
- **Get it** by joining our QQ group `1027673321` and grabbing it from the group files.
- After unzipping, enter `new_last_128_piano/portable/` and double-click `run.bat`; the browser opens `http://127.0.0.1:8081`.

> The open-source source code in this repo is the same pipeline; the bundle simply adds the runtime + trained weights so non-developers can use it immediately.

### Option 2: Source + `run.bat` (Windows)

1. Copy the whole `adofai-diffusion` folder to your machine.
2. Double-click **`run.bat`**.
   - On the first run it creates a virtual environment and `pip install`s all dependencies (may take a few minutes depending on your network).
   - When done, the browser auto-opens `http://127.0.0.1:8081`.
3. In the web UI, **go to the "Train Models" tab and train the models first** (see below), otherwise generation fails because there are no weights.

> If you run from source without `run.bat`:
> ```
> python -m venv venv
> venv\Scripts\activate
> pip install -r requirements.txt
> python app/web_server.py --port 8081
> ```

### Option 3: Docker (Linux / servers)

The repo ships a `Dockerfile` + `docker-compose.yml`, supporting both CPU and GPU:

```bash
# from the repo root
docker compose up -d --build
```

Open <http://localhost:8081>. Training data is mounted as volumes (`./train` / `./data` on the host) and persists across container restarts.

> For detailed Docker setup (GPU acceleration, data directories, FAQ) see **[DOCKER.md](DOCKER.md)** (Chinese).

---

## Generate a chart (3 steps)

1. **Step 1 · Pick audio**: drag or choose a song in the "Generate" tab.
2. **Step 2 · Pick separated stems**: click **Split audio**, wait for it to separate the song into 5 stems (drums / bass / melody-piano / vocals / accompaniment-no-vocals); each has a preview player and a checkbox.
   - Checking **Original audio** disables all separated stems; checking any separated stem disables **Original audio** (they are mutually exclusive).
   - Separated stems can be **multi-selected**.
3. **Step 3 · Generate**:
   - Original audio / a single stem → sent straight to the AI.
   - Multiple stems → auto-merged into one audio, then sent to the AI.
   - When done, download the `.adofai` and the mel-spectrogram preview.

---

## Train your models

The AI's "taste" comes from the chart data you give it. More data, closer to the genres you make, = better results.

### 1. Prepare data

Under `train/`, make one folder per song containing a paired set of files:

```
train/JayChou-Qingtian/qingtian.mp3 + qingtian.adofai
train/HuYanbin-Hongyan/hongyan.ogg + hongyan.adofai
```

- Audio: `.ogg` or `.mp3`.
- Chart: `.adofai` (the "ground truth" you made manually in the ADOFAI editor).

### 2. One-click training

Open the web UI → **Train Models** tab → select your `train` data directory → click **Train all**.
Training produces three weight files in `data/checkpoints/`:

| Weight | What it does | Default epochs |
|--------|--------------|----------------|
| `onset_net.pt` | Onset model (hears the beats) | 80 |
| `vae.pt` | Chart compression/reconstruction | 60 |
| `ddpm.pt` | Diffusion denoising (makes the patterns) | 120 |

> Epochs, learning rate, etc. can be tuned in the web UI or on the command line.

### 3. Or train separately from the command line

```bat
REM Train the onset model
venv\Scripts\python.exe app\training\train_onset.py --train_dir train --epochs 80

REM Train the style model (VAE + diffusion)
venv\Scripts\python.exe app\training\train_stage2.py
```

> See [train/README.md](train/README.md) for the exact training-data layout.

---

## FAQ

**Q: Double-clicking run.bat does nothing / the black window flashes and closes?**
A: You probably don't have Python installed, or it isn't on the system PATH. Install Python 3.12+ and tick "Add to PATH" during setup.

**Q: Generation says "not enough onsets / no chart generated"?**
A: The song may be too sparse for the AI to find beats, or the weights aren't trained well. Try selecting "Original audio / All mixed" instead of a single isolated stem.

**Q: Uploading a .wav fails?**
A: Fixed in the latest code (an old same-name-file conflict). Use the latest source; if it still fails, convert to mp3/ogg first.

**Q: Generation is slow?**
A: Training/inference both use the GPU. Without a discrete GPU, CPU is very slow.

**Q: Can it add camera moves / effects?**
A: The current version only does beats and steps, no visual effects. That's a future enhancement.

**Q: Docker reports "venv missing / model runtime not found"?**
A: Old code hard-coded the Windows venv path; it now auto-detects Linux `venv/bin/python`. Inside the container the default `--in-process` mode bypasses venv entirely — see [DOCKER.md](DOCKER.md).

---

## Technical details

### Requirements

- **OS**: Windows (tested). Other OSes should work in theory, but `run.bat` is Windows-only — use an equivalent command to start the server (or use Docker).
- **Python**: 3.12 or newer (developed on 3.12/3.13).
- **GPU**: NVIDIA discrete GPU recommended (training requires a GPU). Inference can run on CPU but is slow.
- **Internet on first run**: Python dependencies and the Demucs separation model weights must be downloaded once. After that it can run offline.

### Directory structure

```
adofai-diffusion/
├── run.bat                # Windows one-click launcher (builds venv + installs deps on first run)
├── Dockerfile             # Docker image build
├── docker-compose.yml     # Docker orchestration (CPU/GPU, volumes for data/ and train/)
├── DOCKER.md              # Docker deployment docs
├── requirements.txt       # Python dependencies
├── requirements-dev.txt   # Dev/test dependencies
├── README.md              # Chinese documentation (this file's sibling)
├── README_EN.md           # English documentation (this file)
├── LICENSE                # MIT license
├── .gitignore
├── train/                 # Put your own training data here (see train/README.md); not tracked by git
├── tests/                 # Unit tests (test_chart_repr / test_paths / test_timing_engine)
└── app/
    ├── web_server.py          # Web backend (Generate + Train tabs)
    ├── adofai_parse.py        # Read/parse .adofai charts
    ├── onset_detector.py      # Spectrum analysis + preview image (uses ffmpeg to decode audio)
    ├── paths.py               # Path management (data/checkpoint/cache directories)
    ├── timing_engine.py       # Timing engine (angle <-> time; supports Twirl/SetSpeed etc.)
    ├── validate_beat_align.py # Verify beat alignment
    ├── webui/
    │   ├── index.html         # Front-end page (pick audio → pick stems → generate)
    │   └── styles.css         # Front-end styles
    └── training/
        ├── inference_stage2.py  # Main generation flow: audio → onsets + diffusion → .adofai
        ├── onset_net.py         # OnsetNet model definition
        ├── train_onset.py       # Train the onset model
        ├── dataset.py           # Diffusion training dataset (reads train/ pairs)
        ├── train_stage2.py      # Train VAE + diffusion (style model)
        ├── vae.py / diffusion.py# VAE and DDPM diffusion model definitions
        ├── chart_repr.py        # Chart <-> dense representation conversion
        ├── demucs_mel.py        # Demucs source separation + mel features
        ├── separation.py        # Stem separation logic (multi-stem merging, etc.)
        ├── separate_all.py      # Separate all stems (web "split audio" button)
        ├── preview_track.py     # Export one separated stem for preview/playback
        └── eval_onset.py        # Onset model evaluation
```

### About the model weights

The code **does not include** trained weights (`onset_net.pt` / `vae.pt` / `ddpm.pt`).
Reason: the weights are learned from training data, and that data (songs + charts) is copyrighted, so it isn't published alongside the code.
Train them from your own data using the steps above; once trained, drop them into `data/checkpoints/`.

---

## License

[MIT](LICENSE). Use, modify, and redistribute freely, as long as the copyright notice is retained.
