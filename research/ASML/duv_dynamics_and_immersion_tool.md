# Duv Dynamics And Immersion Tool

An **immersion tool** (ArFi) is a DUV lithography system that uses a thin layer of **ultra-pure water** between the lens and the wafer to improve resolution.

### How it works:

In normal "dry" lithography, light travels through air (refractive index = 1.0) from the lens to the wafer. In immersion lithography, the gap is filled with water (refractive index = 1.44). This bends the light more tightly, allowing finer patterns to be printed — effectively shrinking the wavelength from 193nm to ~134nm without needing a new light source.

```
Dry lithography:      Lens → [air gap] → Wafer     (193nm effective)
Immersion lithography: Lens → [water gap] → Wafer   (~134nm effective)
```

### Where it sits in the product stack:

| Tool | Type | ASP | Resolution |
|---|---|---|---|
| EUV (NXE:3800E) | Extreme UV | ~€210M | 13.5nm — best |
| **ArFi (Immersion)** | **DUV + water** | **~€65-95M** | **~134nm effective — 2nd best** |
| ArF Dry | DUV, no water | ~€28M | 193nm |
| KrF | Older DUV | ~€10-20M | 248nm |

### Why it matters for ASML:

- **It's the highest-ASP DUV product** (~€65-95M vs €10-28M for dry/KrF/I-line)
- **Every EUV fab needs immersion tools** — EUV only handles the most critical 5-10 layers; the other 60-80 layers use immersion DUV
- Christophe said demand "**scales with EUV**" — so as EUV goes from 60→80+ units, immersion follows
- ASML's latest immersion tool (NXT:2150) delivers sub-nanometer accuracy at 300+ wafers/hour
- It's the single biggest revenue contributor within the "non-EUV" bucket

Think of it as the **workhorse** of a modern chip fab — not the flashiest tool, but the one that does the most work.


Here's everything DUV-related from the Q1 2026 transcript, organized by topic:

### 1. Non-EUV Guidance Upgraded from "Flat" to "Growth"

**Christophe (opening remarks):**
> *"For non-EUV revenue, we previously expected this to be similar to last year, but given continued demand momentum, **we now expect growth** as a result of customers adding more Deep UV lithography to support their expansion plans."*

This is a meaningful upgrade — at Q4 they guided non-EUV "flattish," now they see actual growth.

### 2. Immersion Supply Chain Fix — The Key Driver of the Guidance Raise

**Roger (responding to Joe from Wells Fargo on what drove the revenue guide up):**
> *"On the last call, we were looking at immersion and said, given the supply chain situation on immersion, **we doubt whether we can get immersion to the level that we had last year.** We've been working extremely hard, and right now we're at the point where we believe we can get **immersion close to the levels that we had last year**, and that would primarily flow to the non-Chinese customers. That's a major driver of the uptick."*

So getting immersion output back on track was a **primary reason** for the guidance raise from €34-39B → €36-40B.

### 3. Immersion Demand Scales with EUV

**Christophe (responding to Sandeep from JP Morgan on DUV capacity):**
> *"When it comes to immersion, we believe that for non-Chinese customers, **the demand on immersion will scale with the demand for EUV**, because as you know, there is a pretty clear relationship between the two. If you add EUV capacity, you would also be adding some immersion capacity."*

This is a critical structural insight: DUV isn't dying — it's co-scaling with EUV. Every advanced fab needs both.

### 4. 600-Unit DUV Capacity Is Sufficient (for now)

**Christophe (same exchange):**
> *"600 is for total DUV, and I think when we look at the total, I think we still feel pretty good about that. A bit like EUV, we have other means to play with that number if needed."*

So they have headroom at 600/year and aren't concerned about needing to expand DUV capacity.

### 5. 2027 DUV Demand Outlook

**Christophe (responding to CJ from Cantor Fitzgerald):**
> *"2027 is a bit far for DUV, so I think the expectation is that the number of DUV tools, the number of immersion tools, **the demand there will scale pretty much with what we see on EUV.**"*

Again reinforcing: EUV up → DUV up. The 60→80+ Low-NA EUV ramp implies proportional DUV growth.

### 6. China Remains at ~20% — All DUV Growth Is Non-China

**Roger (first question):**
> *"China remains at the midpoint around 20%. That view has not changed."*

Since China can only buy DUV tools (export controls block EUV), and China is staying at 20% of total sales, this means the entire DUV growth story is non-China. Chinese DUV demand is stable/declining while leading-edge customers (TSMC, Samsung, Intel) are adding immersion capacity alongside EUV.

### 7. Advanced Packaging — A New DUV Use Case

**Christophe (responding to Goldman Sachs question on hybrid bonding):**
> *"We also announced that we were entering advanced packaging with the **XT:260**. This has been an important tool. We continue to see good traction there."*

The XT:260 is a DUV-class tool targeting advanced packaging (3D integration). This is a **new market** for DUV that didn't exist a few years ago — incremental demand on top of the existing DUV base.

---

### Net DUV Takeaway for Modeling

| Item | Previous View (Q4 2025) | Updated View (Q1 2026) |
|---|---|---|
| FY2026 non-EUV systems | "Flattish" vs FY25 | **Now growing** |
| Immersion output | "Doubt we can match FY25" | **Close to FY25 levels** |
| DUV demand driver | Stable/declining | **Scales with EUV ramp** |
| China DUV | Declining | Still declining (~20% of total) |
| New DUV market | — | Advanced packaging (XT:260) |
| FY2027 DUV | Not discussed | **Scales with 80+ EUV ramp** |

DUV went from "drag that we manage" to "co-pilot of the EUV growth story" in one quarter.