import base64
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional
import google.auth
from google import genai
from google.genai import types
from google.cloud import storage

# 默认配置
PROJECT_ID = "gcr-evaluation-hub"
LOCATION = "global"
MODEL_ID = "gemini-omni-1.1-flash-preview"
API_REVISION = "2026-05-20"

DEFAULT_PERSON_URI = "gs://honor-omni-case/omni_flash_4_veo/国内真人7-1_9x16.png"
FALLBACK_PERSON_URI = "gs://honor-omni-case/omni_flash_4_veo/model_non_celebrity_9x16.png"
DEFAULT_CLOTH_URIS = [
    f"gs://honor-omni-case/omni_flash_4_veo/cloth{i}_9x16.png"
    for i in range(1, 6)
]

# 通用提示词：适用于任意1张人物参考图 + 5张服装参考图
UNIVERSAL_OUTFIT_TRANSITION_PROMPT = """你是一位专业的时尚短视频导演。输入的第1张图片是人物模特参考图，后面的5张图片（第2张至第6张）是5套不同的服装参考图。
请生成一个包含5个连续片段的完整时尚换装视频，并将这5个片段合成为一个连贯的完整视频输出。具体要求如下：

1. 人物与画幅一致性：全程严格保持第1张图片中人物的面部五官、神态气质、发型、身材比例及画面比例（9:16竖屏）高度一致，背景与光影氛围自然协调。
2. 服装精准还原与去噪：在第1至第5个片段中，让第1张图中的人物依次穿上第2张至第6张图片中的对应服装。请精准还原每套服装的版型、颜色、面料材质及领口/袖口/内搭层次细节，同时完全忽略服装参考图中的衣架、原始背景以及任何商品促销文字、品牌Logo水印或装饰贴纸。
3. 适配服装风格的动态表现：根据每套服装的品类与穿搭风格，为人物自动设计自然、得体且贴合服装特性的展示动作与表情（如轻抚衣领、自信微笑、微侧转身展示版型、双手自然插兜或整理衣摆等）。
4. 片段间自然过渡与合成：请先生成人物分别试穿这5套服装的高清定妆图作为视觉锚点，再依次生成对应的5个动态视频片段，确保相邻片段之间的动作姿态、镜头运动和光影衔接流畅自然（例如利用连贯的转身、手部动作或平滑运镜衔接），避免生硬突兀的跳切，最终将5个片段合成为一个完整的视频输出。"""


def _get_field(obj, key: str, default=None):
    """兼容 dict 和 SDK 对象属性读取。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def get_storage_client() -> storage.Client:
    """获取兼容本地 ADC 与 Colab 环境的 GCS Client。"""
    try:
        creds, _ = google.auth.default()
        if hasattr(creds, "with_quota_project"):
            creds = creds.with_quota_project(None)
        return storage.Client(project=None, credentials=creds)
    except Exception:
        return storage.Client()


def download_gcs_bytes(uri: str) -> bytes:
    """从 gs:// URI 下载二进制内容。"""
    if not uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI: {uri}")
    bucket_name, blob_name = uri[len("gs://") :].split("/", 1)
    client = get_storage_client()
    return client.bucket(bucket_name).blob(blob_name).download_as_bytes()


def transfer_gcs_movie_to_local(
    gcs_uri: str,
    local_dir: str = ".",
    local_filename: Optional[str] = None,
) -> str:
    """将 GCS 上的视频文件 (gs://...) 下载到本地目录，方便本地 Debug 与查看。"""
    if not gcs_uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI (must start with gs://): {gcs_uri}")

    bucket_name, blob_name = gcs_uri[len("gs://") :].split("/", 1)
    out_dir = Path(local_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not local_filename:
        local_filename = Path(blob_name).name or "debug_gcs_movie.mp4"
        if not local_filename.endswith(".mp4"):
            local_filename += ".mp4"

    local_path = out_dir / local_filename
    print(f"[Debug GCS -> Local] 正在从 {gcs_uri} 下载视频到本地: {local_path} ...")
    client = get_storage_client()
    blob = client.bucket(bucket_name).blob(blob_name)
    blob.download_to_filename(str(local_path))
    size_mb = local_path.stat().st_size / (1024 * 1024)
    print(f"[Debug GCS -> Local] 下载完成 ({size_mb:.2f} MB): {local_path}")
    return str(local_path)


def _extract_gcs_uris(obj) -> List[str]:
    """递归提取任意 step 结构中出现的 gs:// URI，便于 Debug 追踪。"""
    found: List[str] = []
    if isinstance(obj, str):
        if obj.startswith("gs://"):
            found.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            found.extend(_extract_gcs_uris(v))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found.extend(_extract_gcs_uris(item))
    elif hasattr(obj, "__dict__"):
        for v in vars(obj).values():
            found.extend(_extract_gcs_uris(v))
    return found


def build_interaction_input(
    person_uri: str = DEFAULT_PERSON_URI,
    cloth_uris: Optional[List[str]] = None,
    prompt: str = UNIVERSAL_OUTFIT_TRANSITION_PROMPT,
    mime_type: str = "image/png",
) -> list:
    """组装 gemini-omni-1.1-flash-preview 的多图+通用提示词输入。"""
    if cloth_uris is None:
        cloth_uris = DEFAULT_CLOTH_URIS

    content = [
        {
            "type": "image",
            "mime_type": mime_type,
            "uri": person_uri,
        }
    ]
    for cloth_uri in cloth_uris:
        content.append(
            {
                "type": "image",
                "mime_type": mime_type,
                "uri": cloth_uri,
            }
        )
    content.append(
        {
            "type": "text",
            "text": prompt,
        }
    )
    return [
        {
            "type": "user_input",
            "content": content,
        }
    ]


def concat_videos_ffmpeg(video_paths: List[str], output_path: str) -> bool:
    """当模型返回多个独立分镜视频但未自动拼接时，使用 ffmpeg 无缝合并。"""
    list_file = Path(output_path).parent / "concat_list.txt"
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for vp in video_paths:
                f.write(f"file '{os.path.abspath(vp)}'\n")
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(output_path),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception as e:
        print(f"ffmpeg concat 提示: {e}")
        return False
    finally:
        if list_file.exists():
            list_file.unlink()


def generate_outfit_transition_video(
    person_uri: str = DEFAULT_PERSON_URI,
    cloth_uris: Optional[List[str]] = None,
    prompt: str = UNIVERSAL_OUTFIT_TRANSITION_PROMPT,
    output_dir: str = ".",
    auto_fallback_on_rai: bool = True,
    debug_save_all_gcs_videos: bool = True,
) -> List[str]:
    """调用 gemini-omni-1.1-flash-preview 生成5段换装视频并保存到本地。"""
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    if cloth_uris is None:
        cloth_uris = DEFAULT_CLOTH_URIS

    client = genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location=LOCATION,
        http_options=types.HttpOptions(
            headers={"Api-Revision": API_REVISION},
            timeout=1200000,
        ),
    )

    print(f"正在调用 {MODEL_ID} ...")
    print(f"- 人物参考图: {person_uri}")
    for idx, c_uri in enumerate(cloth_uris, start=1):
        print(f"- 服装参考图 {idx}: {c_uri}")

    try:
        interaction = client.interactions.create(
            model=MODEL_ID,
            input=build_interaction_input(
                person_uri=person_uri,
                cloth_uris=cloth_uris,
                prompt=prompt,
            ),
            timeout=1200.0,
        )
    except Exception as e:
        err_msg = str(e)
        if auto_fallback_on_rai and "prominent individuals" in err_msg:
            print(
                f"\n[提示] 输入的人物图 ({person_uri}) 触发了 Google Responsible AI 公众人物/明星人脸保护策略:\n"
                f"  {err_msg}\n"
                f"正在自动切换为同目录下的非明星真人模特图进行验证: {FALLBACK_PERSON_URI}\n"
            )
            interaction = client.interactions.create(
                model=MODEL_ID,
                input=build_interaction_input(
                    person_uri=FALLBACK_PERSON_URI,
                    cloth_uris=cloth_uris,
                    prompt=prompt,
                ),
                timeout=1200.0,
            )
        else:
            raise

    saved_videos: List[str] = []
    image_idx = 0
    video_idx = 0
    input_uris = {person_uri, FALLBACK_PERSON_URI, *cloth_uris}

    steps = _get_field(interaction, "steps", []) or []
    print(f"\n收到响应，共包含 {len(steps)} 个 step。")

    for s_i, step in enumerate(steps):
        step_type = _get_field(step, "type")
        content = _get_field(step, "content")
        print(f"- Step {s_i}: type={step_type}")

        # Debug: 如果非 model_output step 中包含 GCS 视频 URI，也自动下载到本地 debug 目录
        if debug_save_all_gcs_videos and step_type != "model_output":
            for gcs_uri in _extract_gcs_uris(step):
                if gcs_uri not in input_uris and (
                    gcs_uri.endswith(".mp4") or "video" in gcs_uri.lower()
                ):
                    debug_name = f"debug_step{s_i}_{Path(gcs_uri).name}"
                    transfer_gcs_movie_to_local(
                        gcs_uri, local_dir=str(out_path), local_filename=debug_name
                    )

        if step_type == "model_output" and content:
            for part in content:
                part_type = _get_field(part, "type")
                if part_type == "text":
                    text_val = _get_field(part, "text", "")
                    print(f"\n[Step {s_i} Text]:\n{text_val}")
                elif part_type == "image":
                    image_idx += 1
                    img_b64 = _get_field(part, "data")
                    img_uri = _get_field(part, "uri")
                    img_bytes = None
                    if img_b64:
                        img_bytes = base64.b64decode(img_b64)
                    elif img_uri:
                        print(f"[Step {s_i} Image URI]: {img_uri}")
                        img_bytes = download_gcs_bytes(img_uri)
                    if img_bytes:
                        img_file = out_path / f"omni_keyframe_{image_idx}.png"
                        img_file.write_bytes(img_bytes)
                        print(f"已保存定妆参考图 {image_idx}: {img_file}")
                elif part_type == "video":
                    video_idx += 1
                    vid_b64 = _get_field(part, "data")
                    vid_uri = _get_field(part, "uri")
                    vid_file = out_path / f"omni_video_{video_idx}.mp4"
                    if vid_uri:
                        print(f"[Step {s_i} Video GCS URI]: {vid_uri}")
                        transfer_gcs_movie_to_local(
                            vid_uri,
                            local_dir=str(out_path),
                            local_filename=vid_file.name,
                        )
                        saved_videos.append(str(vid_file))
                    elif vid_b64:
                        vid_bytes = base64.b64decode(vid_b64)
                        vid_file.write_bytes(vid_bytes)
                        saved_videos.append(str(vid_file))
                        print(
                            f"已保存内嵌 Base64 视频 {video_idx} ({len(vid_bytes) / (1024*1024):.2f} MB): {vid_file}"
                        )

    if saved_videos:
        final_video_path = out_path / "final_outfit_transition.mp4"
        last_size = os.path.getsize(saved_videos[-1])
        first_size = os.path.getsize(saved_videos[0])
        if len(saved_videos) > 1 and last_size < first_size * 1.8:
            print("\n检测到返回了多个独立视频片段，正在合并为完整视频...")
            if not concat_videos_ffmpeg(saved_videos, str(final_video_path)):
                shutil.copyfile(saved_videos[-1], final_video_path)
        else:
            shutil.copyfile(saved_videos[-1], final_video_path)
        print(f"\n最终完整视频已保存至本地: {final_video_path}")

    return saved_videos


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="调用 Gemini Omni 生成五段换装视频，或将 GCS 视频下载到本地进行 Debug。"
    )
    parser.add_argument(
        "--download-gcs",
        type=str,
        default=None,
        help="仅将指定的 gs:// 视频 URI 下载到本地目录用于 Debug（例如 gs://honor-omni-case/omni_flash_4_veo/final_outfit_transition.mp4）",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=os.path.dirname(os.path.abspath(__file__)),
        help="本地视频保存目录",
    )
    args = parser.parse_args()

    if args.download_gcs:
        transfer_gcs_movie_to_local(args.download_gcs, local_dir=args.output_dir)
    else:
        generate_outfit_transition_video(output_dir=args.output_dir)

