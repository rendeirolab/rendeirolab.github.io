---
title: "Tissue clocks: how tissue structure tells time"
subtitle: "Our study on reading biological age from histopathology — and from blood — is out in Nature Medicine"
date: 2026-08-14
---

We're thrilled to announce that our study on tissue clocks has been published in [*Nature Medicine*](https://doi.org/10.1038/s41591-026-04566-5)! This is the project that convinced me our lab has a unique angle on aging, and the one I've wanted to write about for a long time.

In this post, I want to take you behind the scenes: the motivation that drove us, the data bottleneck, the Eureka moment, and the long road of validation.

## Clocks that tell time, but not what time it's worth

Biological age clocks are having a moment, and for good reason: they are remarkable predictors. Nonetheless, DNA methylation and plasma proteomics while super powerful, both measure processes that are largely passive: methylation accumulates as the cell's maintenance of the genome slips, while circulating proteins are what organs shed into blood. We still don't understand these processes well, and neither is directly tied to the *fitness of our body*.

There is a well-documented gap between the molecular and cellular changes of aging — decades of research by many people, now synthesized into the [hallmarks of aging](https://doi.org/10.1016/j.cell.2022.11.001) — and the physiological decline we actually experience. These are often studied by different disciplines entirely. We hypothesized that **tissue architecture is the missing bridge**: the integrator of molecular and cellular alterations into structural changes. And the structure of our tissues is intimately tied to their physiological function — think of how gas exchange in the lung depends on an exquisite arrangement of thin walls and capillaries. If we could transform images of tissues into predictors of biological age, those predictors would be directly anchored in physiological capacity.

That's what tissue clocks measure: microscopic patterns associated with aging — the accumulation of fibrosis, the calcification of arteries, the loss of blood capillaries. These patterns depend on each person's genetics, environment, habits, and health. All of these factors accumulate and interact over time in complex ways, and their combined effect on a person's tissues can differ wildly between two individuals born on the same day.

## The bottleneck was always the data

### What was missing

Longitudinal tissue sampling in humans is essentially impossible outside of non-invasive imaging — radiology is wonderful, but it misses the molecular and tissue context. Biopsy collections are plentiful for cancer and disease, but scarce for healthy tissue. HuBMAP was still in its early days when we started, and while a fully molecularly-grounded view via spatial transcriptomics was at the top of my wishlist, it simply wouldn't scale. Studying aging in humans means accounting for demographic, clinical, and lifestyle factors — you need both superb annotation and data at scale.

Make no mistake: the deep learning revolution was essential — the images had to become numbers somehow. But it wasn't the bottleneck. Even a ResNet50 that had never seen a tissue section in its life, with a simple linear model at its head, does fairly well at age prediction. The bottleneck was always the data: the whole human body, richly annotated, at scale.

### Enter GTEx

Then I discovered the **GTEx histopathology collection**. Around a thousand donors who donated their bodies to science, with 40+ tissue locations across 29 organs, consistently annotated, and — crucially — paired with genotypes, bulk RNA-seq, and rich clinical metadata. It is a truly visionary resource, and I want to express my deep gratitude to the original authors and everyone who worked over many years to collect it.

## Turning slides into numbers

Over 25,000 whole slide images, each tiled into thousands of small patches. Vision models trained on massive datasets — first ImageNet, nowadays huge histopathology corpora — are remarkably good at learning comprehensive numeric descriptions of images. We started with ImageNet-trained models, then fine-tuned our own, because despite the explosion of pathology foundation models, there still wasn't one focused on *healthy* tissue embeddings — largely due to the lack of data.

### The Eureka moment

After a long stretch of feature extraction, we finally ran dimensionality reduction on the whole dataset. The UMAP from our paper — Fig. 1c — was **the moment I was convinced** that H&E images could study aging. I made that plot in December 2022, and it genuinely felt like a Eureka moment.

<p align="center">
    <picture align="center">
        <img src="https://cdn.jsdelivr.net/gh/rendeirolab/rendeirolab.github.io@main/assets/img/2026-08-14-tissue_clocks-Fig1c.png" alt="UMAP of 25,000 whole slide images colored by organ and age" width="80%">
    </picture>
</p>

But a Eureka moment is just a hypothesis with good vibes. We spent over six months doing all sorts of analyses just to prove to ourselves that it wasn't an artifact — and more than a year doing the same once we built the clocks themselves. In retrospect it's a pity we "wasted" so much time before diving into downstream analyses, but it was necessary to be certain.

<p align="center">
    <picture align="center">
        <img src="https://cdn.jsdelivr.net/gh/rendeirolab/rendeirolab.github.io@main/assets/img/2026-08-14-tissue_clocks-Fig1d.png" alt="Conceptual scheme of a tissue clock: predicting age from image features, and the age gap" style="max-width: 60%; max-height: 390px;">
    </picture>
</p>

## From pixels to physiology

### Does the gap mean anything?

The clocks themselves are only interesting if the *age gaps* — the residuals between predicted and chronological age — carry biological weight. We designed a series of orthogonal checks. GTEx happens to have telomere length measurements for many of the same tissue samples, exquisite pathology annotations, and disease histories for the donors. All of these pointed the same way: the biological age we captured in the images was associated with shorter telomeres, more subclinical pathology, and higher comorbidity burden — in ways that chronological age alone did not fully capture.

<p align="center">
    <picture align="center">
        <img src="https://cdn.jsdelivr.net/gh/rendeirolab/rendeirolab.github.io@main/assets/img/2026-08-14-tissue_clocks-Fig1j.png" alt="Age gaps decoupled from chronological age" style="max-width: 80%; max-height: 390px;">
    </picture>
</p>

### Taking it on the road

Then came the harder test: did the clocks work outside GTEx? Here our collaborators in Austria, Germany, Belgium, and Australia were fundamental — thank you so much for believing in the idea. For independent cohorts of brain, lung, and skin tissue — processed in completely different manners, scanned on different machines — clocks trained on GTEx generalized, with specificity depending on the tissue they were trained for. The aging signal is in the tissue, not in the scanner.

### What organs tell us

With the clocks in hand, the fun began. We used vision-language models to translate what the clocks see: terms like atrophy, microvascular rarefaction, and fibrosis consistently increased across organs, while epithelial features declined. We found genes whose expression tracks biological age specifically in certain tissues. We characterized how different organs accelerate or decelerate their aging along the adult lifespan — and which demographic, clinical, and lifestyle factors modulate the rate of aging in specific organs. Some people age systemically; others have one prominently aged organ.

## Reading organs from blood

Still, one obvious problem remained: you can't go around sampling tissue from people just to tell them how their organs are doing. Nobody would — or should — undergo that for a number. The answer, once again, came from the dataset itself. Of all the tissues in GTEx, we had so far ignored the blood. Could the blood of donors carry a signal revealing the state of their organs?

It sounds like pure science fiction. It is precisely what we did. Using gene expression measured in blood, we trained predictors of the image-derived age gaps for each tissue — effectively a *second generation* of clocks that no longer learn directly from age, but from the morphological deviation of each organ, and in a tissue-resolved manner.

This was a tall order. Most regression models happily output Gaussian values centered at zero — which is exactly the distribution of our residuals. How could we know we were learning anything real? Again, external data came to the rescue. Using essentially all bulk RNA-seq data the community has ever made available, neatly packaged in the [ARCHS4](https://maayanlab.cloud/archs4/) database, we applied our blood-based predictors to roughly 600 healthy donors and over 500 samples from eight chronic diseases plus stroke. In every case, diseased samples showed higher age gaps than healthy donors. Better still: the signals were enriched in the organs known to be most affected. In Crohn's disease, the GI tract stood out. In Alzheimer's — despite a small sample size — the brain was the only significant organ.

<p align="center">
    <picture align="center">
        <img src="https://cdn.jsdelivr.net/gh/rendeirolab/rendeirolab.github.io@main/assets/img/2026-08-14-tissue_clocks-Fig5.png" alt="Strategy to predict tissue age gaps from blood and disease associations" width="80%">
    </picture>
</p>

## What's next

We hope these predictors will prove useful for disease risk stratification and diagnostics once validated in incident disease — the step from association to prediction is one we take seriously and have not crossed. Concretely, we're working on:

- **Validating in prospective cohorts** with pre-diagnostic blood samples and clinical follow-up, to test whether tissue-specific age gaps precede disease onset
- **Connecting to genetics** through GWAS and Mendelian randomization, moving from correlations toward causal inference
- **Expanding coverage** to larger histopathology repositories and more tissues as they become available

But beyond the tool itself, I hope the approach resonates: appreciating tissue morphology as a principle that connects molecular and cellular alterations of aging to physiology. If our work helps keep the aging field moving in that direction, that's a contribution I'll be proud of.

## Try it yourself

- 📄 [Paper](https://doi.org/10.1038/s41591-026-04566-5)
- 💻 [Analysis code](https://github.com/RendeiroLab/tissue-clocks)
- 📊 [GTEx data](https://gtexportal.org)

## Acknowledgments

This project was led by **Ernesto Abila**, **Iva Buljan**, and **Yimin Zheng** — equal co-first authors. Huge thanks to all co-authors, and the funders who supported this work.

