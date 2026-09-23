# Rusty Blackbird artwork — 2026-09-23

Two exact-taxon illustrations added to repair the first-ever-species alert's missing required asset. These illustrations depict the species, not the acoustic observation or proof that a bird visited. The triggering detection remains a model-conflict candidate.

## Method and sources

Same reference-conditioned Codex native-image workflow used for the prior Carolina Chickadee repair: separate real species photograph and Ohara Koson style reference, one generation per pose, visual/anatomy review, transparent cutouts, silhouette metadata.

- Anatomy: [Euphagus-carolinus-001.jpg](https://commons.wikimedia.org/wiki/File:Euphagus-carolinus-001.jpg), downloaded from https://upload.wikimedia.org/wikipedia/commons/b/b7/Euphagus-carolinus-001.jpg . Visually checked: nonbreeding rusty-edged adult, pale iris, buff eyebrow, slim pointed bill.
- Field marks: [Cornell Lab identification](https://www.allaboutbirds.org/guide/Rusty_Blackbird/id).
- Style only: Ohara Koson, *Cawing Crow*, reproduced at https://www.fabriziomusacchio.com/weekend_stories/told/2024/2024-09-01-ohara_koson/ ; downloaded image https://live.staticflickr.com/65535/53942952963_01105314ee_b.jpg . No crow anatomy or scenery borrowed.
- Autumn rather than breeding plumage deliberately retained for diagnostic species identity.

## Generation and processing

Codex CLI 0.144.1, OpenAI subscription/native image tool, orchestrated by gpt-5.6-sol. The configured gpt-6-astra orchestrator was rejected before generation because this CLI is too old; no CLI upgrade or provider change was made. Native image engine version was not exposed in the retained completion receipt and is not claimed here.

One completed native image generation per pose. A uniform chroma-green background was requested, but the engine returned native RGBA images. Therefore no chroma-key removal was applied. Post-processing only rescales the native alpha maximum of 254 to 255, removes <=2/255 numerical alpha noise, crops to alpha bounds and adds 2% transparent padding. Species colours and anatomy were not procedurally redrawn.

Native source hashes:
- Perched: `c287070105a6b2ff823f6eb8a34d0e20c6ded918eb02980b9d0be12af4ea6ad4`
- Flight: `0e11789cf89c0fc74f74fabb9fb6a3cd4a51f7898efd4e186090002dc5b788c4`

Final cutouts:
- `euphagus-carolinus.png`: 1404 × 1013, SHA-256 `9c57224da3917d647a5ea147e7cbaeafe078aae6cf1f8a59ce1c10a50cb64107`
- `euphagus-carolinus-2.png`: 1354 × 969, SHA-256 `ed2caf9c7e86f558b14bbc78a78780f5604eb162dac9c496c9242c107c756596`

Private generation receipts, exact prompts, original output, reference provenance and processing helper are retained under `~/scratch/rusty-blackbird-art/`. The two prompt texts are preserved beside this note.
