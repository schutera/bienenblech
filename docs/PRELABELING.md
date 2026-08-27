# Prelabeling — VarroDetector as a model-assist source

Reference material and an integration assessment. **Nothing here is built yet**;
this document exists so the decision is made deliberately rather than discovered
halfway through an implementation.

## The upstream project

**VarroDetector** — <https://github.com/jodivaso/varrodetector>
(spelling matters: `varrodetector`, one "a" in the middle; `varroadetector` 404s.)

Counts *Varroa destructor* mites in smartphone photographs of beehive sticky
sheets — the same physical surface this tool is named after. Runs CPU-only, no
install, no network.

| | |
|---|---|
| Weights | `model/weights/best.pt` |
| Architecture | **YOLOv11 nano**, trained on hundreds of sticky-sheet images |
| Task | Object **detection** (axis-aligned boxes) — *not* segmentation |
| License | **AGPL-3.0** |
| Paper | Yániz et al. (2025), *Agriculture* 15(9), 969 — <https://doi.org/10.3390/agriculture15090969> |
| Sample images | SharePoint link in the upstream README (`unirioja-my.sharepoint.com`) |
| Entry point | `python varroa_mite_gui.py`; prebuilt Windows `.exe` on the releases page |

So: **yes, there is a usable trained model, and it is directly on-domain.**

## The licensing question, first

VarroDetector is AGPL-3.0, and so is Ultralytics YOLO11 underneath it. AGPL
section 13 is the clause that matters here: it extends copyleft to *network use*.
Running a covered work as a hosted service obliges you to offer the complete
corresponding source of the combined work to that service's users. Plain GPL
would not do this — distribution triggers GPL, hosting triggers AGPL, and
bienenblech is hosted by design.

Ultralytics also takes the position that weights produced with their trainer are
covered, so "we will just train our own" does not route around it.

Three honest options:

1. **License bienenblech AGPL-3.0** and publish the source. Cleanest, costs
   nothing if the repo was never going to be proprietary.
2. **Keep inference out of the served application.** Run the detector offline as
   a separate tool and import its predictions as a file (see the sketch below).
   Arm's-length data interchange, no covered code in the served binary.
3. **Buy an Ultralytics commercial license** and do not use VarroDetector's own
   weights (a commercial Ultralytics license does not relicense someone else's
   AGPL fine-tune).

This same question already applies to `d:\Projects\cownting`, which depends on
ultralytics and is also hosted.

## What integration would actually look like

The architecture happens to fit well, with one real mismatch.

**Fits.** Crops are 640x640, which is YOLO11's native inference size — one crop
is exactly one inference call, no tiling logic to write twice. The crop queue is
already the unit of work, so a prelabel pass is "for each open crop, run the
model, insert suggested masks".

**Mismatch.** VarroDetector emits *boxes*; this tool stores *polygons*. A box
becomes a 4-vertex rectangle, which is a poor segmentation label. Mites are
roughly elliptical, so an inscribed 8- or 12-vertex ellipse is a better starting
polygon than a rectangle — closer to the true silhouette, and less work for the
annotator to correct than a rectangle that is wrong on all four corners. Either
way the annotator is refining a suggestion, not accepting a mask.

**The risk to name out loud: automation bias.** Prelabels make annotators
rubber-stamp. That is a direct threat to the completeness invariant in
[SPEC.md](SPEC.md) section 1 — the whole reason this tool works on crops. If
prelabeling ships, it must come with:

- `masks.source` (`'human' | 'model'`) and `masks.confidence` on every row, so a
  model-authored label is never silently indistinguishable from a human one, and
  so a later training run can weight or exclude them.
- A visually distinct rendering for unconfirmed model masks (dashed, desaturated).
- An explicit per-mask accept action. Marking a crop `done` should confirm every
  suggestion in it, so "done" keeps meaning a human looked.
- Retention of the original model output, so agreement between model and human
  is measurable later. That number is the honest answer to "is prelabeling
  helping or is it just moving the annotator's errors around?"

**Cost.** The runtime image is deliberately torch-free and small (see
[DEPLOY.md](../DEPLOY.md)). CPU torch + ultralytics adds roughly 1-2 GB. That
argues for option 2 above on engineering grounds as well as legal ones: a
separate optional container, a compose profile, or a purely offline CLI.

## Sketch: the offline, arm's-length path

The variant that avoids both the AGPL network clause and the image bloat.

1. `bienenblech export-crops --status open --out crops/` writes the raw crop JPEGs.
2. VarroDetector runs elsewhere, on its own terms, producing per-crop boxes.
3. `bienenblech import-prelabels predictions.json` inserts them as
   `source='model'` masks with their confidence.

The served application never contains AGPL code and never loads a model. The
interchange format is a plain JSON of `{crop_id, class_id, confidence, points}`.

## Also worth taking regardless

The **sample images** are useful on their own, independent of the model and of
the licensing question: they are real sticky-sheet photographs, which makes them
good seed data for exercising the upload, tiling and labeling flow before any of
your own frames exist. Check the terms attached to the sample set itself before
redistributing them in this repo.
