from pathlib import Path


MODEL = "zipvoice-distill-fp32"
PACK = f"{MODEL}-v1"
MODEL_FILES = (
    Path(MODEL, "text_encoder.onnx"), Path(MODEL, "fm_decoder.onnx"),
    Path(MODEL, "tokens.txt"), Path(MODEL, "lexicon.txt"),
    Path("vocos_24khz.onnx"), Path("lexicon-en-us.txt"), Path("reader-reference-distill-zh.wav"),
)


def missing_files(folder):
    return [path for path in MODEL_FILES if not (folder / path).is_file()]


def locate_models(source_root, executable=None, saved=None):
    candidates = []
    if executable is None:
        candidates.append(source_root / "发布" / "模型库" / PACK)
    else:
        # The version may contain the onedir bundle or be the bundle itself.
        for parent in executable.parents[:3]:
            if parent.name == "程序":
                candidates.append(parent.parent / "模型库" / PACK)
                break
    if saved:
        candidates.append(Path(saved))
    if executable is None:
        candidates.append(source_root / "models")
    return next((path for path in candidates if path.is_absolute() and not missing_files(path)), None)
