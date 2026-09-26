"""training/notebooks/metin2re_qlora.ipynb dosyasını üretir (hücreleri burada düzenleyin, sonra çalıştırın):

    python training/notebooks/build_notebook.py
"""

import json
from pathlib import Path

CELLS = [
    ("markdown", """# Metin2Re QA modeli — QLoRA ince ayar (Kaggle / Colab, ücretsiz GPU)

Bu not defteri `training/export_dataset.py` çıktısını (train.jsonl / val.jsonl) kullanarak küçük bir modeli
Metin2Re QA ajanı olarak eğitir ve **Ollama için GGUF** dosyası üretir.

**Kaggle:** Settings → Accelerator → *GPU T4 x2* (ya da P100), Internet → *On*. Veri kümesini
"Add Input" ile ekleyin (`metin2re-qa-data`).
**Colab:** Çalışma zamanı → Çalışma zamanı türünü değiştir → *T4 GPU*. Aşağıdaki hücre dosyaları yüklemenizi ister.

Süre: ~300 örnekte T4 üzerinde 15–30 dk. Çıktı: `metin2re-qa.Q4_K_M.gguf` (~4.7 GB) → bilgisayarınıza indirip
`python training/import_model.py <gguf>` ile Ollama'ya ekleyin."""),
    ("code", """# Ayarlar — kıyaslamada kazanan temel modeli seçin (training/README.md)
BASE_MODEL = "unsloth/Qwen3-8B-bnb-4bit"   # kıyaslamada kazanan (26 Eylül); diğerleri: unsloth/Qwen2.5-7B-Instruct-bnb-4bit, unsloth/Meta-Llama-3.1-8B-Instruct-bnb-4bit
MAX_SEQ_LEN = 8192          # eğitimde örnek başına en fazla token (uzun olanlar kırpılmaz, atlanır)
EPOCHS = 2
LR = 2e-4
LORA_R = 16
OUT_NAME = "metin2re-qa"
"""),
    ("code", """%%capture
!pip install -q unsloth
!pip install -q --no-deps "trl>=0.12" datasets"""),
    ("code", """import glob, os, json
# Veri: Kaggle girdisi ya da Colab yüklemesi
cands = glob.glob("/kaggle/input/**/train.jsonl", recursive=True)
if cands:
    DATA_DIR = os.path.dirname(cands[0])
else:
    try:
        from google.colab import files
        print("train.jsonl ve val.jsonl dosyalarını seçin")
        up = files.upload()
        DATA_DIR = "."
    except ImportError:
        DATA_DIR = "training/data"
print("veri:", DATA_DIR, os.listdir(DATA_DIR))
man = os.path.join(DATA_DIR, "manifest.json")
if os.path.exists(man):
    print(open(man).read())"""),
    ("code", """from unsloth import FastLanguageModel
model, tokenizer = FastLanguageModel.from_pretrained(BASE_MODEL, max_seq_length=MAX_SEQ_LEN, load_in_4bit=True)
model = FastLanguageModel.get_peft_model(
    model, r=LORA_R, lora_alpha=LORA_R, lora_dropout=0, bias="none",
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    use_gradient_checkpointing="unsloth", random_state=7)"""),
    ("code", """from datasets import load_dataset

def to_text(ex):
    msgs = []
    for m in ex["messages"]:
        m = {k: v for k, v in m.items() if v is not None}
        if m.get("role") == "assistant" and not m.get("tool_calls"):
            m.pop("tool_calls", None)
        msgs.append(m)
    text = tokenizer.apply_chat_template(msgs, tools=ex["tools"], tokenize=False)
    return {"text": text}

ds = load_dataset("json", data_files={"train": os.path.join(DATA_DIR, "train.jsonl"),
                                      "val": os.path.join(DATA_DIR, "val.jsonl")})
ds = ds.map(to_text, remove_columns=ds["train"].column_names)
ds = ds.filter(lambda ex: len(tokenizer(ex["text"]).input_ids) <= MAX_SEQ_LEN)
print(ds)
print(ds["train"][0]["text"][-800:])"""),
    ("code", """from trl import SFTTrainer, SFTConfig
from unsloth.chat_templates import train_on_responses_only

trainer = SFTTrainer(
    model=model, tokenizer=tokenizer, train_dataset=ds["train"], eval_dataset=ds["val"],
    args=SFTConfig(dataset_text_field="text", max_seq_length=MAX_SEQ_LEN, per_device_train_batch_size=1,
                   gradient_accumulation_steps=8, num_train_epochs=EPOCHS, learning_rate=LR, warmup_ratio=0.05,
                   lr_scheduler_type="cosine", logging_steps=5, eval_strategy="epoch", save_strategy="no",
                   optim="adamw_8bit", weight_decay=0.01, seed=7, output_dir="outputs", report_to="none"))
# Yalnızca asistan cevapları (tool çağrıları) öğretilir; sistem/kullanıcı/oyun sonuçları bağlamdır
trainer = train_on_responses_only(trainer, instruction_part="<|im_start|>user\\n",
                                  response_part="<|im_start|>assistant\\n")
stats = trainer.train()
print(stats)
print(trainer.evaluate())"""),
    ("code", """# Hızlı kontrol: eğitilen model geçerli bir tool çağrısı üretiyor mu?
FastLanguageModel.for_inference(model)
ex = json.loads(open(os.path.join(DATA_DIR, "val.jsonl")).readline())
prompt = tokenizer.apply_chat_template(ex["messages"][:-1], tools=ex["tools"], tokenize=False, add_generation_prompt=True)
ids = tokenizer(prompt, return_tensors="pt").to(model.device)
out = model.generate(**ids, max_new_tokens=200, do_sample=False)
print(tokenizer.decode(out[0][ids.input_ids.shape[1]:], skip_special_tokens=False))
print("beklenen:", ex["messages"][-1])"""),
    ("code", """# GGUF (Ollama) çıktısı
model.save_pretrained_gguf(OUT_NAME, tokenizer, quantization_method="q4_k_m")
ggufs = glob.glob(f"{OUT_NAME}*/*.gguf") + glob.glob("*.gguf")
print(ggufs)
target = f"{OUT_NAME}.Q4_K_M.gguf"
os.replace(max(ggufs, key=os.path.getsize), target)
print("hazır:", target, round(os.path.getsize(target) / 1e9, 2), "GB")"""),
    ("code", """# İndirme: Kaggle'da dosya "Output" sekmesinde görünür (/kaggle/working). Colab'da:
try:
    from google.colab import files
    files.download(f"{OUT_NAME}.Q4_K_M.gguf")
except ImportError:
    print("Kaggle: sağdaki Output bölümünden indirin ya da `kaggle kernels output` kullanın")"""),
]


def main() -> None:
    nb = {"nbformat": 4, "nbformat_minor": 5,
          "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
                       "accelerator": "GPU"},
          "cells": []}
    for kind, src in CELLS:
        cell = {"cell_type": kind, "metadata": {}, "source": src.splitlines(keepends=True)}
        if kind == "code":
            cell.update(execution_count=None, outputs=[])
        nb["cells"].append(cell)
    out = Path(__file__).with_name("metin2re_qlora.ipynb")
    out.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
