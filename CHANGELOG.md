# Changelog

<!-- Generated from Git history by git-cliff. Do not edit manually. -->

Release notes for Kensa. Full notes are available on [GitHub Releases](https://github.com/kensa-sh/kensa/releases).

## 0.23.0
### Bug Fixes
* fix(traces): stop asserting on volatile import timestamps ([8c6f685](https://github.com/kensa-sh/kensa/commit/8c6f68502e7486431cb4d1eeae4a8735f58917e8))
* fix(judge): retry transient provider failures ([a945bd0](https://github.com/kensa-sh/kensa/commit/a945bd0da08d3b3bcde747a6f339bc3430c181ab))
* fix(llm): use tenacity for provider retries ([0577659](https://github.com/kensa-sh/kensa/commit/0577659d55a4730c74c3a4b7eb7b6574a6c6052d))
### Chores
* chore(ci): remove spent 0.22.0 changelog verification skip ([71cae96](https://github.com/kensa-sh/kensa/commit/71cae9679798fad4d2a28fff344a1a981fb0f605))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.22.0...v0.23.0


## 0.22.0
### Features
* feat: framework-discovery ([4204eb9](https://github.com/kensa-sh/kensa/commit/4204eb9e499926d9c8fc82485615606f285cb602))
### Bug Fixes
* fix(kensa-setup): invoke detector script with skill-root-relative path ([8f273f3](https://github.com/kensa-sh/kensa/commit/8f273f39cc06938cce54b3a05ae91d898b4afc44))
* fix: update test assertion for relative detector script path ([30f0a9d](https://github.com/kensa-sh/kensa/commit/30f0a9dd5b988bfe07abf1e6e799be5d139fe1f6))
### Chores
* chore(agents): require scoped conventional commits ([c571d23](https://github.com/kensa-sh/kensa/commit/c571d23c4267dcd3e6657581a82fbd0647db14f2))
* chore(docs): remove inline PR links from changelog ([df96569](https://github.com/kensa-sh/kensa/commit/df965697cef5f617be2786ce68e7acd258530c86))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.21.0...v0.22.0

## 0.21.0
### Features
* feat: add interoperable tooling contracts ([be943de](https://github.com/kensa-sh/kensa/commit/be943de7e0ff9436f6fe37ef489d4c51b70a3dc1))
### Bug Fixes
* fix: cover changelog failure gates ([cdf3560](https://github.com/kensa-sh/kensa/commit/cdf3560a8750ec727fcac227bc822f020d90e4c9))
* fix: bind release notes to base commit ([279e410](https://github.com/kensa-sh/kensa/commit/279e41078d23426103a896559dc71300ac8ed70f))
* fix: separate product release notes ([29f224b](https://github.com/kensa-sh/kensa/commit/29f224bc8aef0ba25afb2728f55b4bb35b50a507))
* fix: rebuild changelog from git history ([7f61f81](https://github.com/kensa-sh/kensa/commit/7f61f81ded62265dc9a5b0b5adf125eb4b3779f7))
* fix: isolate lifecycle test environment ([83b427f](https://github.com/kensa-sh/kensa/commit/83b427f1d5119fd409e2294c31c4adc866c109c8))
* fix: generate changelog with git-cliff ([d959358](https://github.com/kensa-sh/kensa/commit/d95935883d65b7987dc195674738bfa734011967))
* fix: require explicit opt-in for OTLP HTTP export ([af0211b](https://github.com/kensa-sh/kensa/commit/af0211bebb704b7bb2cf151635d942a4e12caf5b))
* fix: key OTLP export on a Kensa-owned endpoint and scope credentials ([2da1b6e](https://github.com/kensa-sh/kensa/commit/2da1b6ebb4166c3e7fc92895a2cfe3078c0329de))
### Chores
* chore: synchronize release changelogs ([a180806](https://github.com/kensa-sh/kensa/commit/a180806512b3b4cd6c3ae80e5927e77e812f64f9))
* chore: tighten instrument docstring and OTLP documentation prose ([4346ff1](https://github.com/kensa-sh/kensa/commit/4346ff10fc1a2ea07b4746904c0f11e5f4a54072))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.20.0...v0.21.0

## 0.20.0
### Features
* feat: update default judge models ([17d4a33](https://github.com/kensa-sh/kensa/commit/17d4a331d30fda1a8e772958eeea9efc41a418b7))
### Bug Fixes
* fix: keep setup production-backed ([ccd2d51](https://github.com/kensa-sh/kensa/commit/ccd2d512456b38d9586bbcbf26876af14c07e2d8))
* fix: resolve production setup review ([aed861e](https://github.com/kensa-sh/kensa/commit/aed861e71d11f4a3dd069301d496bedd22164d26))
* fix: use concrete setup language ([72ff594](https://github.com/kensa-sh/kensa/commit/72ff59440e942ddbeb92b4c926784967ed3e6146))
* fix: clamp legacy observation page limit ([0837e7e](https://github.com/kensa-sh/kensa/commit/0837e7efeda7dabdf9c87f1c21692f298de868b0))
### Chores
* chore: remove tests that assert on documentation prose ([762bbd4](https://github.com/kensa-sh/kensa/commit/762bbd4a64987c998fa89cbb3434b2ce68793c24))
* chore: reduce setup documentation bloat ([e09b6ae](https://github.com/kensa-sh/kensa/commit/e09b6ae259d1d56dfac860022d273f14bb72e6ba))
* chore: document Agent Skills guidance ([0e405c9](https://github.com/kensa-sh/kensa/commit/0e405c923aeb0b4f53ff691b068cefbd9e3ef7de))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.19.1...v0.20.0

## 0.19.1
### Features
* feat: structured-tool-evidence ([8b7a112](https://github.com/kensa-sh/kensa/commit/8b7a11286fbf2aea1bcbfe5cc6764742656e2ab3))
### Chores
* chore: update changelog for 0.19.1 ([d4c7d65](https://github.com/kensa-sh/kensa/commit/d4c7d654255918becd4dd1a410dd7b5ceffc80b4))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.19.0...v0.19.1

## 0.19.0
### Breaking Changes
* feat!: versioned-run-results ([c4907cc](https://github.com/kensa-sh/kensa/commit/c4907ccdffcd5df1e55d0fc73db58ad73813a10c))
### Bug Fixes
* fix: validate versioned run results ([d2bffee](https://github.com/kensa-sh/kensa/commit/d2bffee2e2b5a38496e40d606517c15d9399573a))
### Chores
* chore: update changelog for 0.19.0 ([eb4a823](https://github.com/kensa-sh/kensa/commit/eb4a8235fc4a2e7cf4b2ea49ba45052b07d72c0f))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.18.1...v0.19.0

## 0.18.1
### Bug Fixes
* fix: gate releases on live redaction ([3f16511](https://github.com/kensa-sh/kensa/commit/3f16511cc6c42d25441bd24f7db491acb0479e69))
### Chores
* chore: publish releases after manual merge ([cf009d4](https://github.com/kensa-sh/kensa/commit/cf009d46fc362ebab2773fac31d71fad17d97452))
* chore: update changelog for 0.17.0 and 0.18.0 ([c49710c](https://github.com/kensa-sh/kensa/commit/c49710c9de1e5f85c42255a4e27802f2d48419a2))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.18.0...v0.18.1

## 0.18.0
### Breaking Changes
* feat!: trial-failure-provenance ([4635bbc](https://github.com/kensa-sh/kensa/commit/4635bbc5ba2aa701b2eb2a0c77360f8a095bf0cf))
### Bug Fixes
* fix: trial-failure-provenance ([b075d91](https://github.com/kensa-sh/kensa/commit/b075d91500cb714adb1132f9fd17965bda68573d))
* fix: stabilize watchdog timeout test ([fe93786](https://github.com/kensa-sh/kensa/commit/fe9378619e3f8cbe89cd6be9db14f060072e7dc8))
* fix: stabilize watchdog integration tests ([09bcd79](https://github.com/kensa-sh/kensa/commit/09bcd79dedd3c4adda6fd09553119332fd97bb84))
* fix: validate complete judge results ([975e169](https://github.com/kensa-sh/kensa/commit/975e1691b8397efbcea37bde2909ce7337354cc8))
* fix: classify pytest xfails as harness failures ([c10ba64](https://github.com/kensa-sh/kensa/commit/c10ba64d4ac23823526d00f5cf5fa44bc496f4bd))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.17.0...v0.18.0

## 0.17.0
### Features
* feat: external-run-evidence ([bb5479b](https://github.com/kensa-sh/kensa/commit/bb5479bd1dee7f5b0ff3ec658c8cd58ae85b504c))
### Bug Fixes
* fix: validate external run evidence safely ([72acfb0](https://github.com/kensa-sh/kensa/commit/72acfb01e3c0f1e2c9460ccc27f2c9668167ddf7))
* fix: scope external evidence to run task ([cb66de6](https://github.com/kensa-sh/kensa/commit/cb66de697277550c4c1d7b39dff1602e46f3c6fc))
### Chores
* chore: update changelog for 0.16 releases ([9efad5c](https://github.com/kensa-sh/kensa/commit/9efad5c286cb1beb8ca0ac00799776e69e8eb3b2))
* chore: stabilize watchdog phase test ([fc16462](https://github.com/kensa-sh/kensa/commit/fc16462a267c84592bf6087b85c622cd3a5a0614))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.16.1...v0.17.0

## 0.16.1
### Features
* feat: add reliability scoring ([40ecad2](https://github.com/kensa-sh/kensa/commit/40ecad26aaa672d0d0ffd68485f393a376ffdb3d))
### Bug Fixes
* fix: correct reliability scoring edge cases ([88b3ee7](https://github.com/kensa-sh/kensa/commit/88b3ee7a74e058f3f2453c66309334bf2f81231c))
* fix: preserve reliability scoring evidence ([20fcd7a](https://github.com/kensa-sh/kensa/commit/20fcd7ac6507cb8b382e78eb16e860495cc3b5c8))
* fix: align GenAI telemetry conventions ([f4d378a](https://github.com/kensa-sh/kensa/commit/f4d378a5d3ac1861fe90b0834af17c0846dabad7))
* fix: snapshot instrumented GenAI spans ([65863dc](https://github.com/kensa-sh/kensa/commit/65863dcfff507380d7e4f3ac76aa9b3c35138001))
* fix: support GenAI operation names ([be30102](https://github.com/kensa-sh/kensa/commit/be30102653eb07c862551621de08f73c01120af8))
* fix: track active instrumented GenAI spans ([7f1ba44](https://github.com/kensa-sh/kensa/commit/7f1ba44c0ee072269831fcd8ad2a17ed1f70aa84))
### Chores
* chore: auto-merge release pull requests ([77f50eb](https://github.com/kensa-sh/kensa/commit/77f50eb734d77a2d998057bcb216176718460641))
* chore: trim reliability documentation ([7767ab1](https://github.com/kensa-sh/kensa/commit/7767ab1fff68cda0eade99dfa1d222a035d8a685))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.16.0...v0.16.1

## 0.16.0
### Breaking Changes
* feat!: conversational-evals ([680a4b3](https://github.com/kensa-sh/kensa/commit/680a4b3ce46209f0ac211cc282bb383bb116d705))
* feat!: rename conversation result to run result ([5601e10](https://github.com/kensa-sh/kensa/commit/5601e1090ef2e4f53aff23f19a7e812ea72ef3c2))
* feat!: rename RunResult to CaseResult ([5f3cbd8](https://github.com/kensa-sh/kensa/commit/5f3cbd8dbdf4854d5407a993439be516f5a204d7))
### Features
* feat: expose trace on case results ([45190a5](https://github.com/kensa-sh/kensa/commit/45190a5084bfe8b74d63ccbc77c5a1d305722ce5))
### Bug Fixes
* fix: conversational-evals ([c48c7d5](https://github.com/kensa-sh/kensa/commit/c48c7d53babf26249d7478cb7e930f61cc9d8c23))
* fix: seed empty simulator conversations ([25ad6c1](https://github.com/kensa-sh/kensa/commit/25ad6c1a946f65dc6962bc74fcde86decfda352a))
* fix: preserve implemented init harness ([bc0e2eb](https://github.com/kensa-sh/kensa/commit/bc0e2ebb9e036da7842a98653523423610d99bd9))
* fix: classify simulator schema failures ([0a30afe](https://github.com/kensa-sh/kensa/commit/0a30afe6211d7fd71440b269fb5a0b1dc869fc1c))
* fix: keep async spans task-local ([97afe9e](https://github.com/kensa-sh/kensa/commit/97afe9e9b9ee2e3e76ca2247f94c5f799a1e3536))
* fix: support async agents in smoke check ([93753ec](https://github.com/kensa-sh/kensa/commit/93753eca5816f40a389900e8e9963a06e5aa937e))
* fix: classify malformed structured responses ([8228c2e](https://github.com/kensa-sh/kensa/commit/8228c2e7da608a992d5076acc9e0d536573b46e5))
* fix: preserve case result equality ([627ff8d](https://github.com/kensa-sh/kensa/commit/627ff8d50629822a157b2b39decb3acc5430a19f))
### Chores
* chore: update changelog for 0.15.0 ([fa7464a](https://github.com/kensa-sh/kensa/commit/fa7464a0d7e920039c411982f18b06269c71f3a3))
* chore: test live llm tool selection ([0620b7f](https://github.com/kensa-sh/kensa/commit/0620b7f1c8e63a89643556e218f3e5ebb5f370cf))
* chore: test live refund policy simulation ([6140356](https://github.com/kensa-sh/kensa/commit/61403568017264f2b1523cca2d7badcb881b30aa))
* chore: split live simulator tests ([488d405](https://github.com/kensa-sh/kensa/commit/488d405431d1c07808d71c75bd685accc8a6ce7a))
* chore: assert simulator withholds order id ([922b642](https://github.com/kensa-sh/kensa/commit/922b6426e12d1c0559f4eb20051b854ffdb4c73f))
* chore: remove added code comments ([ce2f3a1](https://github.com/kensa-sh/kensa/commit/ce2f3a10342fa8aea1eace87f150f656100a6cb3))
* chore: use result trace in live tests ([1fc5990](https://github.com/kensa-sh/kensa/commit/1fc59904c87868626b02d749f244921224262f29))
* chore: make result trace canonical ([b9aebb1](https://github.com/kensa-sh/kensa/commit/b9aebb1d08f62887963b6749b067f1e3616f3fb3))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.15.0...v0.16.0

## 0.15.0
### Breaking Changes
* feat!: move project config to pyproject.toml ([7f745ad](https://github.com/kensa-sh/kensa/commit/7f745ada93ce1091045c84140c2079657d147a37))
### Features
* feat: repo-aware-run-diagnosis ([707bd85](https://github.com/kensa-sh/kensa/commit/707bd8542233dac305c62f2d7c33d46700f2487e))
### Bug Fixes
* fix: narrow run diagnosis scope ([819a32e](https://github.com/kensa-sh/kensa/commit/819a32e2bf9a3ff3e06c3920216dd4ccd068d483))
* fix: streamline eval console output ([ac0d37b](https://github.com/kensa-sh/kensa/commit/ac0d37b57c3a6e502eb41f65031502d743cb53ab))
* fix: restore eval aggregate headline ([438b841](https://github.com/kensa-sh/kensa/commit/438b8411a7a3d0435042259960d7497ac5546f53))
### Chores
* chore: update changelog ([35fde59](https://github.com/kensa-sh/kensa/commit/35fde59a288329e6ca4deb43456fd01c3069290e))
* chore: document pyproject configuration ([5418c68](https://github.com/kensa-sh/kensa/commit/5418c68f94909a0b99275c41c6fd4e6177a4f40f))
* chore: tighten configuration docs ([890894c](https://github.com/kensa-sh/kensa/commit/890894c4bc8e6859a09eb59ee8f7011fe50d67db))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.14.0...v0.15.0

## 0.14.0
### Breaking Changes
* feat!: parallelize Kensa trials by default ([8a188c6](https://github.com/kensa-sh/kensa/commit/8a188c63efd3bcc06b6296ab656837a4272cbdcf))
### Bug Fixes
* fix: make pytest skip coverage deterministic ([7c59822](https://github.com/kensa-sh/kensa/commit/7c598220c1dc32f5f36ea95e46755aca358b5354))
* fix: preserve parallel timeout evidence ([05f2b0c](https://github.com/kensa-sh/kensa/commit/05f2b0c15feb6bf459177d11220b3715e1bcd6b5))
* fix: prefer latest timeout snapshot ([7604a45](https://github.com/kensa-sh/kensa/commit/7604a453ac27ca46a9fcd329e17428d814622daa))
### Chores
* chore: tighten concurrency docs ([834fa42](https://github.com/kensa-sh/kensa/commit/834fa426418a4d1cad7593823bb6f9d1fa5d4664))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.13.0...v0.14.0

## 0.13.0
### Features
* feat: add hard eval timeouts (#55) ([d2a05d7](https://github.com/kensa-sh/kensa/commit/d2a05d721bea8fad966373e55d2a526255203c72))
### Chores
* chore: migrate docs (#53) ([e9f7a7c](https://github.com/kensa-sh/kensa/commit/e9f7a7cd75ca29ba51abe1d317f77a480cce09ef))
* chore: restore docs changelog (#54) ([efbbe14](https://github.com/kensa-sh/kensa/commit/efbbe14dd3d5aa7b505f4292b1341e21d295f0a3))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.12.0...v0.13.0

## 0.12.0
### Breaking Changes
* feat!: minimize imported trace evidence ([dedaf04](https://github.com/kensa-sh/kensa/commit/dedaf04f7361a2ce71ae55da0548e3dd16875443))
### Bug Fixes
* fix: reject duplicate span ids ([931d38e](https://github.com/kensa-sh/kensa/commit/931d38e070d80a4454cd683c1ed2bd83b7db7025))
* fix: use legacy Langfuse observations API ([89445d9](https://github.com/kensa-sh/kensa/commit/89445d9da90af1772aefc8658572d84988b90984))
* fix: harden minimized trace evidence ([d65c564](https://github.com/kensa-sh/kensa/commit/d65c5645c68ea9ad6140a173f2d6c42b5887dfb9))
* fix: retain current OTLP GenAI evidence ([c95df8c](https://github.com/kensa-sh/kensa/commit/c95df8cd7e6224304a6fa63b0603b086ce1333cb))
* fix: harden redaction initialization ([bded06b](https://github.com/kensa-sh/kensa/commit/bded06bb956539f2ee7a5bd75adf8f7d3a97e2d6))
### Chores
* chore: simplify redaction model wording ([c70b7a8](https://github.com/kensa-sh/kensa/commit/c70b7a8b8d3796bf26565bf82857a423e9fa0812))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.11.1...v0.12.0

## 0.11.1
### Bug Fixes
* fix: streamline redaction setup ([cd672e9](https://github.com/kensa-sh/kensa/commit/cd672e99c174810a0596d7f77ebad1859e70567c))
* fix: install redaction as dev dependency ([7859538](https://github.com/kensa-sh/kensa/commit/785953820d71a2a7d08d5a010c9a4fdb7d0495ba))
* fix: preserve redaction install diagnostics ([d8ce235](https://github.com/kensa-sh/kensa/commit/d8ce235bab5d2fe7085707ce9e403ee559a30919))
* fix: simplify redaction init feedback ([18d7693](https://github.com/kensa-sh/kensa/commit/18d76934180f3ba9c7c809acef81b4d67d24ca47))
* fix: fail incomplete redaction setup ([19e4605](https://github.com/kensa-sh/kensa/commit/19e4605ca539cbeecbc25c712de5b78ffa0983ac))
* fix: isolate agent scaffolding test ([aca9a2a](https://github.com/kensa-sh/kensa/commit/aca9a2a2e84afa80ab833c967c9d44d1401a54f2))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.11.0...v0.11.1

## 0.11.0
### Features
* feat: mandatory trace redaction before every evidence boundary ([f35bad6](https://github.com/kensa-sh/kensa/commit/f35bad64ed694bf9fef0f195303f51f7458355e7))
* feat: simplify mandatory trace redaction ([455658f](https://github.com/kensa-sh/kensa/commit/455658fe935a12d1b7dd04c4fcd0197368899908))
### Bug Fixes
* fix: close trace redaction exposure gaps ([0a7dfcf](https://github.com/kensa-sh/kensa/commit/0a7dfcf19e02841e7491a1581d9ac66d60b5d5a4))
* fix: close remaining trace redaction gaps ([cffcc93](https://github.com/kensa-sh/kensa/commit/cffcc93e36195ea9876a03591d15f4ff9ba8f77c))
* fix: preserve redaction integrity ([d2e2784](https://github.com/kensa-sh/kensa/commit/d2e2784d4abcb0840825cf87dd56f78d167c08c9))
* fix: preserve numeric trace timings ([77439c4](https://github.com/kensa-sh/kensa/commit/77439c4b708cabdf91d1391ee8e2e8272776c846))
* fix: allow deferred local redaction setup ([ff7ccd0](https://github.com/kensa-sh/kensa/commit/ff7ccd0990846d3cead56a3e18cb40da31b888a8))
* fix: bind redaction manifests to trace artifacts ([559dc71](https://github.com/kensa-sh/kensa/commit/559dc711bb097fa3630c86d2bb5fbc12522b55eb))
* fix: harden trace imports ([7001a18](https://github.com/kensa-sh/kensa/commit/7001a18ba2ff839700b1dffb873cd0f17c0307cd))
### Chores
* chore: clean trace read paths ([1ccf507](https://github.com/kensa-sh/kensa/commit/1ccf507448c621ef28c19a3ed869e89104297cb3))
* chore: simplify redaction skill guardrails ([bfc1059](https://github.com/kensa-sh/kensa/commit/bfc10596af060eb31897eeb3607d6bedb417da1d))
* chore: simplify redaction documentation ([9136961](https://github.com/kensa-sh/kensa/commit/9136961d45c140296b60a2ea468d02e9c6f83887))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.10.3...v0.11.0

## 0.10.3
### Bug Fixes
* fix: avoid trace reads during langfuse connection ([640d180](https://github.com/kensa-sh/kensa/commit/640d180906b2c35b233e95f97112a8807f8e777b))
### Chores
* chore: defer langfuse import scope to cli ([9d0f0be](https://github.com/kensa-sh/kensa/commit/9d0f0be2453e0cf9b06aec059cc10079d5a625d8))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.10.2...v0.10.3

## 0.10.2
### Chores
* chore: add langfuse sdk provider adapter ([b716fc4](https://github.com/kensa-sh/kensa/commit/b716fc42a728691dfbb36a12dfc0a61ad6d5d2fa))
* chore: route langfuse cli through provider ([1188378](https://github.com/kensa-sh/kensa/commit/118837869a086480b89032d291d344a2afa4f732))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.10.1...v0.10.2

## 0.10.1
### Bug Fixes
* fix: parse langfuse IO client-side ([65d7e3e](https://github.com/kensa-sh/kensa/commit/65d7e3e1003b3f72b335cd045cf938bd245cc1a7))
* fix: let init connect without judge key ([adf4fe5](https://github.com/kensa-sh/kensa/commit/adf4fe57892fe20fca71cd5791afdfb44bbdf4b9))
* fix: surface langfuse request errors ([076b14c](https://github.com/kensa-sh/kensa/commit/076b14c3216f83ff6280f5096471e247762ee02f))
* fix: tolerate invalid init judge provider ([616f849](https://github.com/kensa-sh/kensa/commit/616f849fdecb6896e6f36d066c381279d8ac3d0f))
* fix: warn on invalid init judge provider ([30295b2](https://github.com/kensa-sh/kensa/commit/30295b28246589237fa8a3690250a77bd5302bbf))
* fix: keep init interactive with invalid judge provider ([08f5f4c](https://github.com/kensa-sh/kensa/commit/08f5f4c4ecb3438aa3c47a39cbc85558f6255d48))
* fix: align langfuse setup parity ([7b1b059](https://github.com/kensa-sh/kensa/commit/7b1b0596797fbbc6487d368197dac6d77c6d6d05))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.10.0...v0.10.1

## 0.10.0
### Features
* feat: support langfuse events-only imports ([5de5416](https://github.com/kensa-sh/kensa/commit/5de5416660023b19ab1f304d6c8fcfc99d5a28c7))
### Bug Fixes
* fix: harden langfuse observations imports ([4ab3e27](https://github.com/kensa-sh/kensa/commit/4ab3e27508d0d5b16a90ebdbbe27c0f47108e410))
### Chores
* chore: trim langfuse import docs ([7256e8a](https://github.com/kensa-sh/kensa/commit/7256e8a181e72d7fbc9278b15dbb33e44aeb90b1))
* chore: revert readme changes ([597b649](https://github.com/kensa-sh/kensa/commit/597b649310f5113fb14e2a999b6ff07ad52ad498))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.9.4...v0.10.0

## 0.9.4
### Features
* feat: verify langfuse import readiness (#31) ([aadfeeb](https://github.com/kensa-sh/kensa/commit/aadfeebfdda27db8399dec9a337d28fc8eb52e18))
### Bug Fixes
* fix: respect langfuse endpoint env (#30) ([e3b13ca](https://github.com/kensa-sh/kensa/commit/e3b13caa4bb80e4bebf75e58f4a3f29ed6a949d0))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.9.3...v0.9.4

## 0.9.3
### Bug Fixes
* fix: use absolute URLs for banner and license link in README (#27) ([c225b29](https://github.com/kensa-sh/kensa/commit/c225b29da9919197f0e65533a2d0dad85569e65c))
* fix: simplify tty agent picker (#28) ([433787c](https://github.com/kensa-sh/kensa/commit/433787ca3cf1717b44d1bbfacc44a0750160a297))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.9.2...v0.9.3

## 0.9.2
### Features
* feat: add init agent onboarding choices (#25) ([7dc63f6](https://github.com/kensa-sh/kensa/commit/7dc63f6691d2419ba1313804ab1380ae2ca2533a))
### Chores
* chore: automate release note labels (#22) ([7d8c0c2](https://github.com/kensa-sh/kensa/commit/7d8c0c2c222a8eeec732739200e9c6bd6f288092))
* chore: lead README install with single agent fetch line (#23) ([80a28e2](https://github.com/kensa-sh/kensa/commit/80a28e25c14541d0199c40b8a0aa49b79ba63cad))
* chore: add legacy version callout and modify README (#24) ([4728157](https://github.com/kensa-sh/kensa/commit/47281571cd94dc6059632ec6a3a77ca75a5c6bb2))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.9.1...v0.9.2

## 0.9.1
### Bug Fixes
* fix: relax package dependency bounds (#16) ([83b3df9](https://github.com/kensa-sh/kensa/commit/83b3df9530dc329df5a3f2ca3e8c92c0dec6000a))
### Chores
* chore: remove examples (#15) ([57f7ea9](https://github.com/kensa-sh/kensa/commit/57f7ea9c1a03df50b9e4f7d8f43e47265ded925d))
* chore: align release labels with commit prefixes (#20) ([684555d](https://github.com/kensa-sh/kensa/commit/684555dca7ec21feec807f77c1d7a51f5019f997))

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.9.0...v0.9.1

## 0.9.0
### Features
* feat: add pytest evals harness ([ff2b6df](https://github.com/kensa-sh/kensa/commit/ff2b6df6d68ed5f66625dd14080274ecc9c35f23))
### Bug Fixes
* fix: remove direct redaction model dependency ([f0bee9f](https://github.com/kensa-sh/kensa/commit/f0bee9f9cc0cb635e35498cd901107d11110b9ac))
### Chores
* chore: remove gitleaks workflow ([bb30a95](https://github.com/kensa-sh/kensa/commit/bb30a9554f080d433ea3da7bcaaa62cd88bdd1a3))
* chore: restore pypi environment in release workflow (#11) ([0174247](https://github.com/kensa-sh/kensa/commit/0174247555084c5df553287a7a4bf243470c3004))
* chore: update maintainer email (#12) ([19932e9](https://github.com/kensa-sh/kensa/commit/19932e9a5767c916ddf8c37a547c64c886c9fdd0))
* chore(deps-dev): bump coverage from 7.14.1 to 7.15.0 (#6) ([7facd31](https://github.com/kensa-sh/kensa/commit/7facd319789087d82c06f3f744cf7b5b9b19e650))
* chore(deps-dev): bump ruff from 0.15.15 to 0.15.20 (#7) ([2516f70](https://github.com/kensa-sh/kensa/commit/2516f7041f7527fc05fb8ce0e8fa7321ebfed72d))
* chore(deps): bump click from 8.4.1 to 8.4.2 (#9) ([02faaa2](https://github.com/kensa-sh/kensa/commit/02faaa2d6d6431cb2b280e8802d7eaada3a49adf))
* chore(deps): bump opentelemetry-sdk from 1.42.1 to 1.43.0 (#8) ([e2260b0](https://github.com/kensa-sh/kensa/commit/e2260b0fbc99ce52f427000c1dc5478dacbe6c50))
* chore(deps): bump any-llm-sdk from 1.15.0 to 1.19.0 (#5) ([bb3151b](https://github.com/kensa-sh/kensa/commit/bb3151bd9d27bdddfd771beeb1b3ac9de2b78f2d))
* chore(deps): bump actions/checkout from 6 to 7 (#2) ([f77b38b](https://github.com/kensa-sh/kensa/commit/f77b38bf640e1ed31fe2422b9eb486b71b1eed06))
* chore(deps): bump actions/upload-artifact from 4 to 7 (#1) ([c788073](https://github.com/kensa-sh/kensa/commit/c78807355641179bd8bf390382a51793e4f2c171))
* chore(deps): bump actions/download-artifact from 4 to 8 (#3) ([a1a106f](https://github.com/kensa-sh/kensa/commit/a1a106fe2a6b21501a2e8a375456f958c848eae7))
* chore: add release notes config for generated changelog (#13) ([9936be1](https://github.com/kensa-sh/kensa/commit/9936be18a2ca19f79550fa7e63245f5705e10bd5))

**Full Changelog**: https://github.com/kensa-sh/kensa/commits/v0.9.0
