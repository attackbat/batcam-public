# B.A.T.C.A.M. Development Kit

[![Kickstarter Campaign](https://img.shields.io/badge/Kickstarter-Live%20Now-brightgreen?style=for-the-badge&logo=kickstarter)](https://www.kickstarter.com/projects/attackbat/1808890155)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)

> 📡 **PROJECT ANNOUNCEMENT:** The evaluation hardware for this development platform is officially open for backing. Secure an assembled prototype board or raw component tiers here: **[Get the B.A.T.C.A.M. Devkit on Kickstarter](https://www.kickstarter.com/projects/attackbat/1808890155)**.

---

## 🛠️ System Overview

The **B.A.T.C.A.M. Development Kit** is a modular, edge-computing evaluation platform built for open-source AI vision processing and decentralized peer-to-peer telemetry networking. 

### Core Architecture & Dependencies
* **MCU:** Seeed Studio XIAO ESP32-S3 (Chip Revision v0.2) paired with native `esp32-camera` stacks.
* **Memory Configuration:** Optimized using a non-standard `huge_app.csv` partition layout providing a 3.1MB application payload window to support memory-intensive local models without hitting OTA boundary limits.
* **Decentralized Infrastructure:** Native zero-trust routing integration using C++ base layers for **Husarnet P2P overlay meshes** and centralized workstation access via secure **Tailscale** tunnels.
* **Extensible I/O Breakout:** Built-in 2.54mm (0.1") header expansion layout separating programmatic lines (SIOC/SIOD, RESET, PWDN, XCLK) for advanced integration with peripheral sensor nodes, haptic targets, or the wrist-worn wrist computer setups.

---

## 🚀 Back This Project

This environment is entirely dedicated to data sovereignty and custom open-source design. If you're utilizing these layout files, code modules, or dashboard components on your test bench, consider supporting the production phase.

### Available Tiers Include:
* **Bare-Metal Kit:** High-grade raw carrier PCBs for custom through-hole field assembly and component soldering.
* **Prototype Evaluation Board:** Hand-assembled, fully integrated testing platform featuring pre-soldered interfaces and a modular chassis footprint.

### 🔗 Crowdfunding Registry Link
To guarantee persistent routing through potential title modifications or custom page configurations, use our secure permanent link database hook:
👉 **[https://www.kickstarter.com/projects/attackbat/1808890155](https://www.kickstarter.com/projects/attackbat/1808890155)**
