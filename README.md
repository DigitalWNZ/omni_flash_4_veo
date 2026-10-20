# Omni Flash 4 Veo — Multi-Segment & Start-End Frame Video Transitions

使用 `gemini-omni-1.1-flash-preview` 实现多段时尚换装与跨时空首尾帧平滑过渡视频生成的示例工程。

## 核心脚本与功能

1. **[`omni_outfit_transition.py`](omni_outfit_transition.py) & [`omni_outfit_transition.ipynb`](omni_outfit_transition.ipynb)**
   - **功能**：输入 **1 张人物图 + 5 张服装图**，通过通用提示词一次性生成包含 5 个片段、动作贴合服装风格且过渡自然的完整换装视频（[`final_outfit_transition.mp4`](final_outfit_transition.mp4)）。
   - **运行**：
     ```bash
     python3 omni_outfit_transition.py
     ```

2. **[`omni_auto_style_transition.py`](omni_auto_style_transition.py)**
   - **功能**：仅输入 **1 张人物图**（无服装图），由 Omni 根据人物气质自动设计搭配 **5 套不同风格的穿搭**，并生成 5 段自然过渡的完整换装视频（[`final_auto_style_transition.mp4`](final_auto_style_transition.mp4)）。
   - **运行**：
     ```bash
     python3 omni_auto_style_transition.py
     ```

3. **[`omni_pair_growth_transition.py`](omni_pair_growth_transition.py)**
   - **功能**：输入 **3 组首尾帧图片**（每组 2 张），结合**侧身/转身面部过渡设计（Design the Movement）**、**运动中服装流转（Clothing Transition）**与**浅水平弧线跟拍/时空流转运镜（Background Transition）**生成 3 个平滑过渡视频。
   - **运行**：
     ```bash
     # 默认运行（包含转身/侧脸过渡限制）
     python3 omni_pair_growth_transition.py

     # 对比实验（去掉人物面部不直接正对镜头限制，输出 *_no_face_away.mp4）
     python3 omni_pair_growth_transition.py --no-face-away
     ```
