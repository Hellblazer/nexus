### Fixed

- **The first MinerU PDF index on a fresh install fetches MinerU's pipeline weights before any page is
  parsed, as a step it prints.** Before, the download (about 1.2 GB) ran inside the first page's 180 s
  budget, so on a slow network the server was cut off mid-download and the first pages came out
  degraded. `nx index pdf`, `nx index repo`, `nx collection reindex`, `nx dt index`, `nx dt capture` and
  `nx mineru start` now look for the weights in the Hugging Face cache, with no network access, and
  fetch them when they are missing; an install that already has them prints nothing. A failed fetch
  names the model and the error and exits 75 with the retry command. `nx index repo` instead holds its
  PDFs back with that message, indexes the rest and ends with exit 75; it fetches only when a PDF
  needs extracting, and under `NX_PDF_STACK=refuse` or `pdf_stack.auto_install: false` (the
  post-commit hook) holds the PDFs without downloading. The MinerU restart in `nx upgrade` never
  fetches. The release battery's fresh-install leg now parses a formula PDF through MinerU on the
  candidate wheel, with an empty model cache, before the tag. `MINERU_MODEL_SOURCE=local` or `modelscope`, and a
  `pdf.mineru_server_url` on another host, are unchanged (nexus-7t3w1, F34).
