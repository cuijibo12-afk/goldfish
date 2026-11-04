"""Explicit install action: fetch pinned public BGE files; no chat/credentials."""
import argparse, hashlib, json
from pathlib import Path
from huggingface_hub import hf_hub_download
p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
revision='75c43b069aac4d136ba6bc1122f995fedcfd2781'
expected={'onnx/model.onnx':'69a0b846f4f116b5e6aabf9546ea6754d02264f3211a13a1bd69b31b8040749a','tokenizer.json':'48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26'}
for name,digest in expected.items():
    path=Path(hf_hub_download('Xenova/bge-small-zh-v1.5',name,revision=revision,local_dir=a.output))
    if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise SystemExit('Downloaded model checksum mismatch')
print('Pinned real embedding model verified.')
