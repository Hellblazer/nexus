# Third-party notices: MinerU pipeline weights

These release assets are an unmodified copy of the model files MinerU's `pipeline` backend loads, from
`opendatalab/PDF-Extract-Kit-1.0` at revision `ed6b654c018d742e65a17671e379c5e6ecc87ec9`
(<https://huggingface.co/opendatalab/PDF-Extract-Kit-1.0/tree/ed6b654c018d742e65a17671e379c5e6ecc87ec9>).
Each file keeps its upstream bytes; `MANIFEST.tsv` lists every file's path, size and sha256. An asset's name is the
file's path under `models/` with `/` written `__` (`Layout__PP-DocLayoutV2__model.safetensors` is
`models/Layout/PP-DocLayoutV2/model.safetensors`).

The bundle labels itself `agpl-3.0` (the license block at the end of its README); the full text is in
`LICENSE-AGPL-3.0.txt`. The models in it come from the projects below, each under Apache License 2.0
(`LICENSE-Apache-2.0.txt`).

| Model | Directory under `models/` | Upstream | License |
| --- | --- | --- | --- |
| PP-DocLayoutV2 (layout) | `Layout/PP-DocLayoutV2` | <https://huggingface.co/PaddlePaddle/PP-DocLayoutV2> | Apache-2.0 |
| UniMERNet small (formula recognition) | `MFR/unimernet_hf_small_2503` | <https://github.com/opendatalab/UniMERNet> | Apache-2.0 |
| PP-OCRv4 / v5 / v6 (text detection and recognition), PyTorch conversions | `OCR/paddleocr_torch` | <https://github.com/PaddlePaddle/PaddleOCR>; conversion: <https://github.com/frotms/PaddleOCR2Pytorch> | Apache-2.0 (both) |
| PP-LCNet_x1_0_doc_ori (page orientation) | `OriCls/paddle_orientation_classification` | <https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_doc_ori> | Apache-2.0 |
| PP-LCNet_x1_0_table_cls (table classification) | `TabCls/paddle_table_cls` | <https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_table_cls> | Apache-2.0 |
| SLANet_plus (table structure) | `TabRec/SlanetPlus` | <https://huggingface.co/PaddlePaddle/SLANet_plus> | Apache-2.0 |
| unet.onnx (table structure) | `TabRec/UnetStructure` | <https://www.modelscope.cn/models/RapidAI/RapidTable>, via <https://github.com/RapidAI/TableStructureRec> | Apache-2.0 |

Each Hugging Face link above is the model's upstream home; the copy published here is the one in the
`opendatalab/PDF-Extract-Kit-1.0` link at the top.
