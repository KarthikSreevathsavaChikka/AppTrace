"""Download the pinned text embedding model into the project-local cache."""
import json
from pathlib import Path
from sentence_transformers import SentenceTransformer

root = Path(__file__).resolve().parents[1]
cfg = json.loads((root / 'embedding_model.json').read_text())
model = SentenceTransformer(cfg['model_id'], revision=cfg['revision'],
    cache_folder=str(root / '.models'), device='cpu', trust_remote_code=False,
    config_kwargs={'vision_config': None, 'audio_config': None})
vector = model.encode(['My withdrawal has been pending for weeks.'],
    prompt_name='Retrieval-document', normalize_embeddings=True)
print(f'Model ready: {cfg["model_id"]} @ {cfg["revision"]}; dimensions={vector.shape[1]}')
