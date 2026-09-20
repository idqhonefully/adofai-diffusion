# train_data/ — 训练数据目录（不入库）

本目录用于存放你自己的训练数据，默认是空的。
Docker / compose 会把这里挂载到容器内 `/app/train_data`；
本地训练用环境变量 `ADOFAI_TRAIN_DIR` 指到任意路径也可以。

> 本目录的内容被 `.gitignore` 忽略，**只有这个 `README.md` 会进仓库**，
> 所以你放的音频和谱面不会被误提交。

## 数据格式（train_onset / stage2 通用）

每一首歌 = 一个子文件夹，里面放**同名**的音频和谱面：

```
train_data/
├── MySongA/
│   ├── MySongA.mp3        # 或 .ogg / .wav（优先级 ogg > mp3 > wav）
│   └── MySongA.adofai     # ADOFAI 官方谱面文件
└── MySongB/
    ├── MySongB.ogg
    └── MySongB.adofai
```

要求：
- 子目录名可以随意，但音频与 `.adofai` 文件名主名最好一致（取各自排序第一个）。
- 一首歌只要音频+谱面齐全就会被自动收集，无需清单文件。
- `--train_dir` 支持多目录：用系统路径分隔符（Windows 是 `;`，Linux 是 `:`）
  分隔，例如 melody 一套、vocal 一套分开训练再合起来。

## 训练命令示例

```bash
# 拍点模型（onset）
python app/training/train_onset.py --train_dir ./train_data --epochs 80

# 谱面扩散主模型（VAE + DDPM 一起练）
# 注意：train_stage2.py 的默认目录是 <仓库根>/train（仓库里没有这个目录），
# 必须用 ADOFAI_TRAIN_DIR 显式指到 train_data/
ADOFAI_TRAIN_DIR=train_data python app/training/train_stage2.py

# 摆形状 / 特效模型
python app/training/train_shape.py --data ./train_data
python app/training/train_vfx.py --data ./train_data --cache ./train_data/.vfx_cache
```

> Windows PowerShell 里设环境变量的写法：
> `$env:ADOFAI_TRAIN_DIR="train_data"; python app/training/train_stage2.py`

其它脚本的默认数据目录同样与仓库目录不一致（`train_onset.py` 默认
`<仓库根>/train_single/melody`、`train_shape.py` 默认 `<仓库根>/train`、
`train_vfx.py` 默认 `<仓库根>/train_vfx`），**建议永远显式传目录**，
不要依赖默认值。

## 版权提醒

仓库不附带任何音乐和谱面。请只使用你有权使用的音频做训练。
