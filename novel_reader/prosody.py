import re


HAN = re.compile(r"[\u3400-\u9fff]+")
WETTS_PACK = "wetts-baker-bert-v1"


class ChineseProsody:
    def __init__(self, folder):
        import onnxruntime as ort

        # BERT token IDs are physical line numbers. splitlines() also splits
        # the U+2028 entry in this vocabulary, shifting every later token ID.
        with (folder / "vocab.txt").open(encoding="utf-8") as file:
            self.vocab = {line.strip(): i for i, line in enumerate(file)}
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(folder / "final.onnx"), options,
                                            providers=["CPUExecutionProvider"])

    def boundaries(self, text):
        import numpy as np

        boundaries = {}
        for match in HAN.finditer(text):
            run = match.group()
            if len(run) < 2:
                continue
            # Two positions in BERT's 512-token window are reserved.
            for start in range(0, len(run), 510):
                chars = run[start:start + 510]
                ids = [self.vocab.get(c, self.vocab["[UNK]"]) for c in chars]
                tokens = [self.vocab["[CLS]"], *ids, self.vocab["[SEP]"]]
                scores, = self.session.run(["prosody_output"], {"input": np.array([tokens], dtype=np.int64)})
                # Skip CLS and the final character: punctuation already bounds each run.
                for j, rank in enumerate(scores[0, 1:len(chars)].argmax(axis=1), 1):
                    if rank:
                        boundaries[match.start() + start + j] = int(rank)
        return boundaries

    def prepare(self, text, boundaries=None):
        if boundaries is None:
            boundaries = self.boundaries(text)
        return "".join(c + ("，" if i < len(text) and boundaries.get(i, 0) >= 2 else "")
                       for i, c in enumerate(text, 1))
