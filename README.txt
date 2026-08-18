ADOFAI Diffusion 一体包（离线 0 配置 · 已含 GPU 加速）
================================================

一句话：把这个文件夹整个拷到任意 Windows 机器（有 NVIDIA 显卡），
双击 run.bat 就能用，不用装 Python、不用装 torch、不用联网。

它做什么：上传一首音乐，自动生成一张 ADOFAI 谱面（.adofai），
含踩点、Twirl 翻身走线、以及 VAE/扩散生成的视觉特效。

怎么用：
  1) 双击 run.bat —— 会自动起本地服务并打开浏览器
     （地址 http://127.0.0.1:8081 ）
  2) 「生成」页签：选音频文件 -> 设 BPM / 难度等 -> 点生成 -> 下载 .adofai
  3) 「训练」页签（可选）：选你自己的「音频+谱面」配对文件夹 -> 一键训踩点 / 训风格 / 全训

技术路线：
  - 音频先用 Demucs 做 6 通道分离（鼓/贝斯/其他/人声/原曲/伴奏），更利踩点
  - OnsetNet 在 hop=128 网格上踩点（≈5.8ms 精度）
  - VAE + DDPM 扩散，在训练数据上学习「风格层」（Twirl 走线 + 视觉特效）
  - 已用本机 RTX 4070 训好权重，直接可用；无独显会自动退到 CPU

目录说明：
  app/web_server.py      生成 + 训练 Web 服务（后端）
  app/webui/             生成界面（前端）
  app/training/          模型代码（onset_net / vae / diffusion / train_* / inference_stage2）
  data/checkpoints/      训练好的权重 onset_net.pt / vae.pt / ddpm.pt / vfx_net.pt / shape_model.pt
  torch_hub/             Demucs 预训练模型缓存（离线加载用，请勿删除）
  venv/                  torch 运行环境（含 GPU 版 torch 2.11 + librosa + numpy）
  python/                嵌入式 Python（跑 Web 后端 + ffmpeg）
  train/                 训练数据目录（默认空；在「训练」页签里指向你自己的数据文件夹）

说明：
  - 全离线，不联网、不装环境，双击即用。
  - 训练数据越多风格越丰富（建议 200+ 首「音频+谱面」配对）。
  - 若换电脑拷贝，整个文件夹一起拷，保持目录结构不变即可。
