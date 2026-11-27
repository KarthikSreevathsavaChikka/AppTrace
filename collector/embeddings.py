"""Local, versioned review embeddings. No sentiment or risk inference."""
import hashlib
import json
from pathlib import Path
import threading

MODEL_FILE = Path(__file__).resolve().parents[1] / 'embedding_model.json'
MODEL_LOCK = threading.Lock()
INFERENCE_LOCK = threading.Lock()
_model = None


def spec():
    return json.loads(MODEL_FILE.read_text())


def model_key():
    cfg = spec()
    return f"{cfg['model_id']}@{cfg['revision']}:retrieval-v1"


def text_content(review):
    return ' '.join(str(review.get('content') or '').split())


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def get_model():
    global _model
    with MODEL_LOCK:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            import torch
            torch.set_num_threads(4)
            cfg = spec()
            _model = SentenceTransformer(cfg['model_id'], revision=cfg['revision'],
                cache_folder=str(MODEL_FILE.parent / '.models'), local_files_only=True,
                device='cpu', trust_remote_code=False,
                config_kwargs={'vision_config': None, 'audio_config': None})
        return _model


def encode(texts, query=False):
    import numpy as np
    with INFERENCE_LOCK:
        result = get_model().encode(texts, batch_size=8, normalize_embeddings=True,
            prompt_name='Retrieval-query' if query else 'Retrieval-document',
            convert_to_numpy=True, show_progress_bar=False)
    result = np.asarray(result, dtype='<f4')
    if result.ndim != 2 or result.shape[1] != 768 or not np.isfinite(result).all():
        raise ValueError('Model returned invalid embedding vectors')
    return result
