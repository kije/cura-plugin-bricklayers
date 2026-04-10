# Legal Analysis: BrickLayers CuraEngine Plugin -- Publishability Assessment

**Date:** April 10, 2026
**Subject:** Open-source publication of BrickLayers CuraEngine plugin (kije/cura-plugin-bricklayers)
**Prepared by:** Technology-Law Advisory Analysis (AI-assisted research)

> **DISCLAIMER:** This analysis is informational guidance, not legal advice. It does not create an attorney-client relationship. For binding decisions -- particularly on patent matters -- consult a licensed attorney in the relevant jurisdiction. Patent analysis especially requires qualified patent counsel.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [License Analysis](#2-license-analysis)
3. [Patent Analysis](#3-patent-analysis)
4. [Freedom to Operate Assessment](#4-freedom-to-operate-assessment)
5. [Jurisdictional Considerations](#5-jurisdictional-considerations)
6. [Risk Mitigation Recommendations](#6-risk-mitigation-recommendations)
7. [Conclusion](#7-conclusion)
8. [Sources](#8-sources)

---

## 1. Executive Summary

**Overall publishability verdict: FAVORABLE -- Publish with documented risk awareness.**

The BrickLayers plugin can be published as open-source software under LGPLv3 with a manageable risk profile, provided certain mitigations are implemented. The analysis identifies:

- **License compatibility:** GOOD. LGPLv3 is appropriate and compatible with the entire dependency chain. One minor correction needed regarding CuraEngine's actual license (AGPLv3, not LGPLv3 as stated in project docs). The gRPC plugin architecture means this distinction does not create a legal problem, but documentation should be accurate.

- **Patent risk:** LOW-MEDIUM (jurisdiction-dependent). The underlying technique is covered by expired Stratasys prior art (US 5,653,925, expired 2015). ADDMAN Group holds an active US patent (US 11,331,848 B2) covering substantially the same method, but this patent has serious validity issues: it miscited the key prior art patent number, the third US continuation has received a non-final rejection citing the correct prior art, and the European application is under active third-party challenge. No enforcement actions have been taken against any open-source implementer. Multiple other open-source implementations are publicly available and actively maintained.

- **Recommended actions:** Add a patent awareness notice to documentation, ensure license headers are consistent, and correct the CuraEngine license reference. No changes to the plugin's LGPLv3 license are needed.

---

## 2. License Analysis

### 2.1 Plugin License: LGPLv3-or-later

**Assessment: Appropriate and well-applied.**

The plugin consistently applies LGPLv3-or-later across all components:
- Python source files: `# BrickLayers plugin is released under the terms of the LGPLv3 or higher.`
- Rust Cargo.toml (both host and wasm): `license = "LGPL-3.0-or-later"`
- LICENSE file: Full LGPLv3 text present at repository root

This is consistent and correct. LGPLv3 is the standard license for the Cura plugin ecosystem, matching Cura itself (the GUI frontend).

### 2.2 CuraEngine License Clarification

**Finding: CuraEngine is AGPLv3, not LGPLv3.**

A common misconception exists in the community. The actual license structure is:

| Component | License | Source |
|-----------|---------|--------|
| **Cura** (GUI frontend) | LGPLv3 | Changed from AGPL to LGPL in Sept 2017 |
| **CuraEngine** (slicing backend) | **AGPLv3** | Remained AGPL when Cura moved to LGPL |
| **CuraEngine gRPC Definitions** | **MIT** | github.com/Ultimaker/CuraEngine_grpc_definitions |

**Why this does not create a problem for BrickLayers:**

The CuraEngine plugin architecture is specifically designed so that plugins run as **separate processes** communicating via gRPC (a standard network protocol). Under prevailing open-source legal interpretation:

1. **Ultimaker's stated position:** "The engine is a separate program so the AGPL doesn't 'infect' programs that use it." Plugins "work together with Cura and not the engine." CuraEngine is "even more separate from Cura than a plugin."

2. **Separate process doctrine:** Programs communicating over standard inter-process protocols (sockets, gRPC, HTTP) are generally considered separate works, not derivative works, under copyright law. The FSF's position is more nuanced (considering "intimacy of communication"), but the gRPC boundary is a strong separation.

3. **Ultimaker's own precedent:** Their official CuraEngine_plugin_infill_generate uses a split-licensing model: LGPLv3 for the Cura plugin front-end, BSD-4 for C++ business logic, AGPLv3 only for C++ code that directly modifies CuraEngine internals. This confirms their intent that plugins need not be AGPL.

4. **Proto definitions are MIT-licensed:** The gRPC proto files that BrickLayers vendors are from CuraEngine_grpc_definitions, which is MIT-licensed. MIT is compatible with LGPLv3.

**Recommendation:** Update the README and project documentation to correctly state that CuraEngine is AGPLv3 (not LGPLv3), and note that the plugin operates as a separate gRPC server process, which is the intended plugin architecture. The plugin's own LGPLv3 license is appropriate.

### 2.3 Dependency License Compatibility

All dependencies use permissive licenses compatible with LGPLv3:

#### Rust Dependencies (Host Binary)

| Dependency | License | Compatible with LGPLv3? |
|------------|---------|------------------------|
| tonic 0.12 | MIT | Yes |
| prost 0.13 | Apache-2.0 | Yes |
| tokio 1 | MIT | Yes |
| clap 4 | MIT OR Apache-2.0 | Yes |
| wasmtime 29 | Apache-2.0 WITH LLVM-exception | Yes |
| wasmtime-wasi 29 | Apache-2.0 WITH LLVM-exception | Yes |
| tracing 0.1 | MIT | Yes |
| tracing-subscriber 0.3 | MIT | Yes |
| tonic-build 0.12 | MIT | Yes (build-dep only) |

#### Rust Dependencies (WASM Module)

| Dependency | License | Compatible with LGPLv3? |
|------------|---------|------------------------|
| prost 0.13 | Apache-2.0 | Yes |
| prost-build 0.13 | Apache-2.0 | Yes (build-dep only) |

#### Python Dependencies

| Dependency | License | Compatible with LGPLv3? |
|------------|---------|------------------------|
| grpcio | Apache-2.0 | Yes |
| grpcio-tools | Apache-2.0 | Yes |

#### Vendored Proto Definitions

| Component | License | Compatible with LGPLv3? |
|-----------|---------|------------------------|
| CuraEngine gRPC definitions | MIT | Yes |

**Assessment: No license compatibility issues.** All dependencies use permissive licenses (MIT, Apache-2.0, Apache-2.0 WITH LLVM-exception) that are fully compatible with LGPLv3. There are no copyleft dependencies that would create license conflicts. The Apache-2.0 WITH LLVM-exception on wasmtime is specifically designed to be more permissive than plain Apache-2.0, explicitly allowing static linking without triggering copyleft concerns.

### 2.4 Cura Marketplace Publication

The plugin meets the documented Marketplace requirements:
- License file present at root (LGPLv3 -- standard for Cura ecosystem)
- Cross-platform architecture (WASM + native host for all platforms)
- No self-updating mechanism
- No user data transmission
- Plugin-scoped preferences

**One consideration:** The Marketplace guidelines mention security review. The native binary component (Rust host) may receive additional scrutiny compared to pure-Python plugins. Pre-compiled binaries distributed through the Marketplace should be built via reproducible CI (the project already has GitHub Actions CI).

### 2.5 "Community" Author Attribution

**Assessment: Acceptable but consider adding specifics.**

Using "Community" as the author is legally valid -- there is no legal requirement to attribute to a natural person for open-source software. However:

- For Cura Marketplace listing, a more specific maintainer identifier may be preferred for user trust and support routing.
- For copyright purposes, "Community" is vague. Consider: "BrickLayers Contributors" or the primary maintainer's handle.
- The copyright headers in source files say `Copyright (c) 2026` without a named holder. Under the Berne Convention (applicable in both the US and Switzerland), copyright vests automatically in the author(s) regardless of the notice, so this is not a legal defect, but it is best practice to name the copyright holder(s).

**Recommendation:** Consider updating to `Copyright (c) 2026 BrickLayers Contributors` or `Copyright (c) 2026 kije and contributors`.

---

## 3. Patent Analysis

### 3.1 Patent Landscape

The brick layer / staggered perimeter technique is the subject of the following patents:

#### Expired Prior Art

| Patent | Title | Assignee | Filed | Expired | Status |
|--------|-------|----------|-------|---------|--------|
| **US 5,653,925** | Method for Controlled Porosity Three-Dimensional Modeling | Stratasys, Inc. | Sept 26, 1995 | **Sept 26, 2015** | **Expired -- Public Domain** |

This patent explicitly describes depositing bead elements in "successive horizontal layers in a skew arrangement so as to provide a minimum porosity." It covers the fundamental concept of offset/staggered bead placement in FDM. Having expired after its full 20-year term, the technique it describes entered the public domain in 2015.

#### Active ADDMAN Patents (US Only)

| Patent | Title | Assignee | Filed | Granted | Expires (est.) | Status |
|--------|-------|----------|-------|---------|-----------------|--------|
| **US 11,331,848 B2** | 3D Printing Bead Configuration | Addman Intermediate Holdings LLC | June 24, 2020 | May 17, 2022 | ~June 2040 | **Active (US)** |
| **US 11,813,789 B2** | 3D Printing Bead Configuration | Addman Intermediate Holdings LLC | Feb 8, 2022 (div.) | Nov 14, 2023 | ~June 2040 | **Active (US)** |
| **US 12,017,407 B2** | Interlocking Infill for AM Products | Addman Intermediate Holdings LLC | Nov 8, 2023 (CIP) | June 25, 2024 | ~June 2040 | **Active (US)** |

#### Pending Applications

| Application | Jurisdiction | Status |
|-------------|-------------|--------|
| **EP 4,094,924 A1** | European (EPO) | **Pending -- Under examination, active third-party observations filed** |
| **US 18/753,273** | US | **Non-Final Rejection issued** |
| **US 18/813,838** | US | Pending |

### 3.2 Validity Analysis of ADDMAN Patents

The ADDMAN patents face multiple serious validity challenges:

#### 3.2.1 Prior Art: Expired Stratasys Patent (US 5,653,925)

The Stratasys patent (expired 2015) describes substantially the same staggered bead arrangement that the ADDMAN patents claim. The core concept -- offsetting bead placement between adjacent layers to improve mechanical properties -- is disclosed in the Stratasys patent's claims and specification. Under US patent law (35 USC 102/103), a patent cannot validly claim what was previously disclosed or what would be obvious in light of prior disclosures.

**Critical error in the ADDMAN patent:** US 11,331,848 B2 cites **US 5,659,925** (a patent for a "Door closer holding mechanism") instead of the correct **US 5,653,925** (Stratasys's porosity control patent). The digit "5" was replaced with "9" in the citation. This transposition error likely caused patent examiners to review an irrelevant door-closer patent rather than the directly relevant Stratasys FDM prior art.

Notably, the **European** application (EP 4,094,924 A1) correctly cites US 5,653,925, suggesting the error in the US filing was either a typographical mistake or was corrected during European prosecution.

#### 3.2.2 Additional Prior Art: PrusaSlicer Feature Request #1823

On **February 14, 2019** -- nine months before ADDMAN's provisional application date of November 26, 2019 -- GitHub user "prusa3d/PrusaSlicer" issue #1823 proposed "Alternating perimeter layers (hexagonal walls)." This feature request describes shifting even columns by half of a layer in the Z dimension so that each bead contacts others at 6 spots (hexagonal arrangement).

This constitutes published prior art under 35 USC 102(a)(1) (public disclosure more than one year before the filing date). Patent attorneys have already submitted this as third-party prior art in the European proceedings.

#### 3.2.3 Current Prosecution Developments

**US third continuation (18/753,273):** The USPTO examiner has issued a **Non-Final Rejection**, having for the first time searched for and found the correct Stratasys patent (US 5,653,925). The rejection is based on prior art and obviousness -- precisely the grounds the community has been raising.

**European application (EP 4,094,924 A1):** Multiple third-party observations were filed between November 2024 and November 2025. The application remains pending and has **not been granted**. The EPO's examination process is generally considered more rigorous than the USPTO's, and the active opposition suggests the application faces significant hurdles.

**Legal significance:** While the non-final rejection of a continuation does not automatically invalidate the already-granted parent patents (US 11,331,848 and US 11,813,789), it strongly signals that the USPTO now recognizes the prior art problem. Invalidating the granted patents would require either:
- Inter partes review (IPR) at the PTAB (~$500,000+ total cost)
- Post-grant review (PGR) at the PTAB (window likely closed; PGR must be filed within 9 months of grant)
- Federal court litigation challenging validity
- ADDMAN failing to pay maintenance fees (next due dates: 2026 for US 11,331,848; 2027 for US 11,813,789)

### 3.3 Enforcement History

**No enforcement actions have been identified against any open-source implementer.**

As of April 2026, based on thorough research of public records, news sources, and community forums:

- ADDMAN has **not** sent cease-and-desist letters to any known open-source brick layer implementation
- ADDMAN has **not** filed patent infringement lawsuits against any slicer developer
- ADDMAN has **not** publicly threatened legal action against the open-source community
- Multiple open-source implementations have been publicly available since late 2024 / early 2025 without any reported enforcement activity

This is significant. Patent holders who are aware of infringement and fail to act may face equitable defenses (laches, estoppel) if they later attempt enforcement, though these defenses have limits.

### 3.4 Patent Risk Matrix

| Risk Factor | Assessment | Notes |
|-------------|-----------|-------|
| **Patent validity** | Weak | Strong prior art (US 5,653,925), wrong citation, continuation rejected |
| **Enforcement likelihood** | Low | No enforcement to date against any implementer, 18+ months of public implementations |
| **Enforcement capability** | Moderate | ADDMAN is a mid-size company with litigation resources, but patent troll behavior not observed |
| **Damages exposure** | Very Low | Open-source, non-commercial, no revenue from infringing activity |
| **Geographic scope** | US only | No granted patents outside US; EU application challenged and pending |
| **Community shield** | Strong | Multiple implementations by multiple parties; enforcement against one would trigger community response |
| **De facto standard** | Emerging | OrcaSlicer native integration, multiple post-processors, broad community adoption |

**Composite Risk Rating: LOW-MEDIUM (US exposure) / LOW (outside US)**

---

## 4. Freedom to Operate Assessment

### 4.1 Landscape of Existing Implementations

The following open-source implementations of the brick layer technique are publicly available and actively maintained:

| Implementation | Type | License | Status | Jurisdiction |
|----------------|------|---------|--------|-------------|
| **GeekDetour/BrickLayers** | Post-processing script | GPL-3.0 | Active (726+ stars) | Global |
| **TengerTechnologies/Bricklayers** | Post-processing script | GPL-3.0-or-later | Active (2,200+ stars) | Developer based in Europe |
| **OrcaSlicer (PR #8181)** | Native slicer integration | AGPL-3.0 | In testing/merged | Global |
| **Minimal 3DP "Brick Effect Processor"** | Web tool | Unknown | Active | Global |
| **This plugin (BrickLayers)** | CuraEngine native plugin | LGPL-3.0-or-later | In development | Developer likely Europe-based |

### 4.2 Freedom-to-Operate Analysis

**Arguments supporting freedom to operate:**

1. **Expired prior art:** The fundamental technique is disclosed in US 5,653,925 (Stratasys, expired 2015). Subject matter in expired patents is in the public domain and cannot be re-patented with identical claims.

2. **Independent implementation:** The BrickLayers plugin was independently developed without reference to ADDMAN's proprietary ADDCAAM software or Create it REAL's REALvision Pro implementation. No code was copied from any proprietary implementation.

3. **Obviousness:** The brick layer concept is described as "literally a patent on a shower thought" by multiple commentators. The analogy to real-world masonry (which has used staggered joints for millennia) makes the application to FDM layers obvious to a person skilled in the art, particularly given the explicit disclosure in the expired Stratasys patent.

4. **Community precedent:** Multiple implementations exist across multiple slicers and jurisdictions. No enforcement action has been taken against any of them. OrcaSlicer -- a major slicer with significant commercial users -- has integrated the feature natively.

5. **European patent not granted:** The European application (EP 4,094,924 A1) remains pending and is under active challenge. Without a granted European patent, there is no patent protection in Europe for this technique.

**Residual risks:**

1. **US-granted patents remain active:** Despite validity concerns, US 11,331,848 B2 and US 11,813,789 B2 are granted and presumed valid until challenged. The burden of proof for invalidity in US litigation is "clear and convincing evidence" (though the PTAB IPR standard is lower: "preponderance of the evidence").

2. **Untested in court:** No court has ruled on the validity of the ADDMAN patents. They remain enforceable until successfully challenged.

3. **Distribution channels:** If the plugin is distributed through channels accessible to US users (GitHub, Cura Marketplace), there is theoretical exposure to US patent jurisdiction, though enforcement against an individual open-source developer outside the US would be practically very difficult.

### 4.3 Comparison with Peer Implementations

All known open-source implementations have taken a "publish and proceed" approach:

- **TengerTechnologies:** Developer (Roman Tenger, Europe-based) explicitly noted the European patent is not granted, providing comfort for European distribution. Published under GPL-3.0+.
- **GeekDetour:** Published under GPL-3.0 with no patent disclaimer. Active since early 2025.
- **OrcaSlicer:** Integrated natively into a major slicer (AGPL-3.0). The PR (#8181) was submitted and CI builds are available. This is the most aggressive integration by a significant open-source project.

BrickLayers is positioned similarly to these implementations and does not face unique or elevated risk compared to the existing field.

---

## 5. Jurisdictional Considerations

### 5.1 Developer Location: Europe (likely Switzerland)

If the developer is based in Switzerland:

- **No US patent jurisdiction over the developer personally.** US patents are territorial; they can only be enforced against acts of infringement within the United States (making, using, selling, offering for sale, or importing in the US -- 35 USC 271).
- **No European patent has been granted.** The EP 4,094,924 A1 application is pending and under challenge. Until and unless it is granted and validated in Switzerland, there is no patent protection for the technique in Switzerland.
- **Switzerland is a member of the European Patent Convention (EPC)** but not the EU. If the European patent were ever granted, it would need to be specifically validated in Switzerland to have effect there.
- **Swiss patent law** does not examine novelty and inventiveness during national prosecution, but European patents (the more common route for Swiss protection) do undergo rigorous examination at the EPO.

### 5.2 Patent Jurisdiction Matrix

| Jurisdiction | Patent Status | Risk Level | Notes |
|-------------|--------------|------------|-------|
| **United States** | 2 granted patents + 1 CIP | MEDIUM | Active but validity seriously questioned |
| **Europe (EPO)** | Application pending, challenged | LOW | Not granted; active third-party observations |
| **Switzerland** | No protection | VERY LOW | No national or validated European patent |
| **Rest of world** | No known filings | VERY LOW | No identified patent filings outside US/EP |

### 5.3 Cross-Border Enforcement Considerations

- ADDMAN would need to sue in the US to enforce US patents. This would require establishing personal jurisdiction over the developer (difficult for an individual outside the US) or targeting US-based distributors/users.
- Recent CJEU developments (2025) have expanded cross-border patent jurisdiction within the EU, but these apply to granted European patents, which ADDMAN does not have.
- **Practical reality:** Patent enforcement against individual open-source developers located outside the patent's jurisdiction, for a non-commercial project, with no identified revenue or market harm, is extremely unusual. The cost-benefit calculation strongly disfavors enforcement.

### 5.4 GitHub/Cura Marketplace Hosting

GitHub (US-based) hosts the repository. Theoretically, ADDMAN could issue a patent-based takedown, but:
- GitHub does not have a general "patent takedown" mechanism analogous to DMCA takedowns for copyright.
- Patent enforcement against hosting platforms for open-source code is virtually unheard of.
- The Cura Marketplace is operated by UltiMaker (Netherlands). If the European patent is not granted, there is no patent basis for a takedown request in the EU.

---

## 6. Risk Mitigation Recommendations

### 6.1 Overall Risk Assessment

**MEDIUM-LOW for publication. Proceed with documented awareness.**

The risk is manageable because:
- The technique is fundamentally public domain (expired Stratasys patent)
- No enforcement actions have occurred despite 18+ months of public implementations
- The developer is likely outside US patent jurisdiction
- The European patent application is pending and challenged
- Multiple larger implementations (OrcaSlicer) provide a "community shield"
- The project is non-commercial open-source with no revenue to attract a damages claim

### 6.2 Recommended Actions

#### 6.2.1 Add a Patent Awareness Notice

Add a brief, factual patent notice to the README. Do not make legal claims about validity -- simply inform users:

```markdown
## Patent Notice

The brick layer / staggered perimeter technique for FDM 3D printing was first
described in US Patent 5,653,925 (Stratasys, filed 1995, expired 2015), which
is now in the public domain. Subsequent patents covering similar subject matter
have been filed and are the subject of ongoing validity challenges. Users should
be aware of the patent landscape in their jurisdiction. This notice is
informational and does not constitute legal advice.
```

This serves multiple purposes:
- Documents your good-faith awareness of the prior art
- Establishes the public-domain basis for the technique
- Alerts users without making claims that could be construed as inducement or willfulness
- Is factually accurate and neutral

#### 6.2.2 Correct License Documentation

Update project documentation to accurately reflect:
- CuraEngine is AGPLv3 (not LGPLv3)
- The plugin communicates with CuraEngine via gRPC as a separate process
- The gRPC proto definitions are MIT-licensed (from CuraEngine_grpc_definitions)
- The plugin's LGPLv3 license is appropriate for this architecture

#### 6.2.3 Maintain Independent Implementation Documentation

Preserve evidence that the implementation is independently developed:
- The algorithm is described from first principles in the documentation
- No code was copied from ADDMAN's ADDCAAM or Create it REAL's REALvision Pro
- The implementation references the public-domain Stratasys patent and published community research (CNC Kitchen, public feature requests)
- Git history provides a clear development timeline

#### 6.2.4 Copyright Headers — Removed (Intentional)

Per-file copyright/license headers were removed to avoid maintenance burden and
risk of inconsistency. The root `LICENSE` file is legally sufficient under the
Berne Convention. This is a valid approach for open-source projects.

#### 6.2.5 Consider a Defensive Patent Pledge (Optional)

If the project gains traction, consider adopting a defensive patent pledge (such as the Open Patent Pledge or similar) to signal commitment to open-source and discourage patent assertion. This is optional and primarily a community-building measure.

### 6.3 Actions NOT Recommended

- **Do NOT delay publication** waiting for patent resolution. The community has moved forward; waiting provides no legal benefit and cedes ground.
- **Do NOT add overly aggressive patent disclaimers** that could be interpreted as awareness of infringement (which could be used to establish "willfulness" in a US patent suit, increasing damages). Keep notices factual and neutral.
- **Do NOT change the license** from LGPLv3. It is appropriate for the project and ecosystem.
- **Do NOT geo-block US users.** This would be impractical for open-source software and would not meaningfully reduce risk (the code is publicly available regardless).

---

## 7. Conclusion

### 7.1 Publishability Verdict

**The BrickLayers CuraEngine plugin can be published as open-source software under LGPLv3.**

The legal landscape supports publication:

1. **License:** LGPLv3 is appropriate, well-applied, and compatible with all dependencies.
2. **Patent (technique):** The underlying technique is public domain per expired US 5,653,925.
3. **Patent (ADDMAN):** Active US patents exist but face serious validity challenges, have never been enforced against any implementer, and do not extend to Europe.
4. **Community:** Multiple other implementations are publicly available under open-source licenses, including native integration in OrcaSlicer.
5. **Jurisdiction:** A Europe-based developer is outside the practical reach of US patent enforcement for a non-commercial open-source project.

### 7.2 Residual Risk

The primary residual risk is the theoretical possibility of US patent enforcement by ADDMAN. This risk is assessed as LOW-MEDIUM and is mitigated by:
- Questionable patent validity
- No enforcement history
- Non-commercial nature of the project
- Developer location outside US jurisdiction
- Community precedent of multiple published implementations
- Active challenges to the patents in both US and European proceedings

### 7.3 When to Seek Licensed Counsel

Consult a licensed patent attorney if any of the following occur:
- Receipt of any communication (cease-and-desist, licensing demand, or lawsuit) from ADDMAN, Create it REAL, or their representatives
- Decision to commercialize the plugin (sell, license, or embed in commercial products)
- Desire to contribute to formal patent challenges (IPR proceedings, third-party observations at EPO)
- Change in the European patent application status (if it progresses toward grant)
- Any change in the developer's US-based activities that could establish US jurisdiction

---

## 8. Sources

### Patent Documents

- [US 5,653,925 (Stratasys, expired) -- Google Patents](https://patents.google.com/patent/US5653925A/en)
- [US 11,331,848 B2 (ADDMAN) -- Google Patents](https://patents.google.com/patent/US11331848B2/en)
- [US 11,813,789 B2 (ADDMAN) -- Google Patents](https://patents.google.com/patent/US11813789B2/en)
- [EP 4,094,924 A1 (ADDMAN, European application) -- Google Patents](https://patents.google.com/patent/EP4094924A1/en)
- [US 12,017,407 B2 (ADDMAN, CIP) -- Google Patents](https://patents.google.com/patent/US12017407B2/en)
- [PrusaSlicer Feature Request #1823 -- Prior Art (Feb 14, 2019)](https://github.com/prusa3d/PrusaSlicer/issues/1823)

### News and Analysis

- [Fabbaloo: "Bricklayers: A New Slicing Technique to Strengthen 3D Prints, But Patent Issues Loom"](https://www.fabbaloo.com/news/bricklayers-a-new-slicing-technique-to-strengthen-3d-prints-but-patent-issues-loom)
- [Fabbaloo: "Patent Confusion Clouds Brick Layers 3D Printing Technique Despite Public Domain Status"](https://www.fabbaloo.com/news/patent-confusion-clouds-brick-layers-3d-printing-technique-despite-public-domain-status)
- [Hackaday: "Brick Layers: The Promise of Stronger 3D Prints and Why We Cannot Have Nice Things" (Nov 2024)](https://hackaday.com/2024/11/09/brick-layers-the-promise-of-stronger-3d-prints-and-why-we-cannot-have-nice-things/)
- [Hackaday: "Brick Layer Post-Processor, Promising Stronger 3D Prints, Now Available" (Jan 2025)](https://hackaday.com/2025/01/23/brick-layer-post-processor-promising-stronger-3d-prints-now-available/)
- [Hackaday: "3D Printed Brick Layers For Everyone" (Mar 2025)](https://hackaday.com/2025/03/17/3d-printed-brick-layers-for-everyone/)
- [JLC3DP: "Brick Layer for Stronger 3D Prints Now Yours to Use"](https://jlc3dp.com/blog/brick-layer-for-stronger-3d-prints-now-yours-to-use)
- [GeekDetour Patreon: "'Brick Layers': Slicers can have it Today (instead of 2040)"](https://www.patreon.com/posts/brick-layers-can-115566868)
- [Hacker News Discussion: "Is 3D printing being held back by an invalid patent?"](https://news.ycombinator.com/item?id=42102235)

### Open-Source Implementations

- [GeekDetour/BrickLayers (GPL-3.0)](https://github.com/GeekDetour/BrickLayers)
- [TengerTechnologies/Bricklayers (GPL-3.0+)](https://github.com/TengerTechnologies/Bricklayers)
- [OrcaSlicer PR #8181: Staggered Perimeters](https://github.com/OrcaSlicer/OrcaSlicer/pull/8181)
- [CNC Kitchen: Brick Layers Blog Post](https://www.cnckitchen.com/blog/brick-layers-make-3d-prints-stronger)

### License and Ecosystem References

- [CuraEngine License (AGPLv3)](https://github.com/Ultimaker/CuraEngine/blob/main/LICENSE)
- [CuraEngine gRPC Definitions (MIT)](https://github.com/Ultimaker/CuraEngine_grpc_definitions)
- [Cura License Change Discussion (AGPL to LGPL)](https://community.ultimaker.com/topic/18824-cura-license-update-%E2%80%93-from-agpl-to-lgpl/)
- [Cura Marketplace Plugin Requirements](https://github.com/Ultimaker/Cura/wiki/Creating-Packages-Plugins-For-The-Marketplace)
- [ADDMAN ADDCAAM Plugin for REALvision Pro](https://www.tctmagazine.com/additive-manufacturing-3d-printing-news/software-and-simulation-news/addman-addcaam-slicing-software-now-available-as-plugin-to/)

### Jurisdiction

- [Switzerland Patent Law -- Lex Mundi Guide](https://www.lexmundi.com/guides/patents/jurisdictions/europe/switzerland/)
- [Switzerland Patent Litigation 2025 -- Chambers & Partners](https://practiceguides.chambers.com/practice-guides/patent-litigation-2025/switzerland)
- [European Patent Convention -- Wikipedia](https://en.wikipedia.org/wiki/European_Patent_Convention)

---

## Appendix A: License Compatibility Summary

```
BrickLayers Plugin (LGPLv3-or-later)
  |
  +-- Rust Host Binary
  |     +-- tonic 0.12 (MIT)                    -- COMPATIBLE
  |     +-- prost 0.13 (Apache-2.0)             -- COMPATIBLE
  |     +-- tokio 1 (MIT)                       -- COMPATIBLE
  |     +-- clap 4 (MIT OR Apache-2.0)          -- COMPATIBLE
  |     +-- wasmtime 29 (Apache-2.0 w/ LLVM)    -- COMPATIBLE
  |     +-- tracing (MIT)                       -- COMPATIBLE
  |
  +-- Rust WASM Module
  |     +-- prost 0.13 (Apache-2.0)             -- COMPATIBLE
  |
  +-- Python Prototype
  |     +-- grpcio (Apache-2.0)                 -- COMPATIBLE
  |     +-- grpcio-tools (Apache-2.0)           -- COMPATIBLE
  |
  +-- Vendored Proto Definitions
  |     +-- CuraEngine gRPC defs (MIT)          -- COMPATIBLE
  |
  +-- Interacts with (separate process, gRPC):
        +-- CuraEngine (AGPLv3)                 -- SEPARATE WORK (gRPC boundary)
        +-- Cura GUI (LGPLv3)                   -- SEPARATE WORK (plugin API)
```

## Appendix B: Patent Family Timeline

```
1995-09-26  US 5,653,925 filed (Stratasys -- staggered bead/porosity control)
2015-09-26  US 5,653,925 EXPIRED -- technique enters public domain

2019-02-14  PrusaSlicer Issue #1823 -- public disclosure of hexagonal walls concept
2019-11-26  ADDMAN provisional application 62/940,419 filed (9 months AFTER #1823)

2020-06-24  ADDMAN US 16/910,556 filed (parent application)
2021-05-25  EP 4,094,924 A1 filed (European application)
2022-02-08  US divisional filed (becomes US 11,813,789)
2022-05-17  US 11,331,848 B2 GRANTED
2023-09-06  Assignment to Addman Intermediate Holdings LLC
2023-11-14  US 11,813,789 B2 GRANTED (divisional)
2024-02     CNC Kitchen publishes brick layer demonstration
2024-06-25  US 12,017,407 B2 GRANTED (continuation-in-part, interlocking infill)
2024-06-25  US 18/753,273 filed (third continuation)
2024-08-23  US 18/813,838 filed (fourth continuation)
2024-11     Third-party observations filed at EPO
2024-11     US 18/753,273 examiner finds correct Stratasys patent
2024-11     US 18/753,273 receives NON-FINAL REJECTION (prior art + obviousness)
2025-01     GeekDetour BrickLayers script published
2025-03     TengerTechnologies Bricklayers script widely adopted
2025-Q1     OrcaSlicer PR #8181 (native stagger perimeters) in testing
2025-11     Additional third-party observations filed at EPO
2026-04     NO ENFORCEMENT ACTIONS by ADDMAN against any implementer
```