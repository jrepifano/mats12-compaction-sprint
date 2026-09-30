# I1a P2 judge pilot: random verdicts per family

seed 0, 10 per family, drawn uniformly from all judged pilot cells


## multihop (10 of 10 judged cells)

- **jlens** `atomic-79-symbol|L44`
  - readout: [" abbreviation", "符号", " Symbol", " symbol", "symbol", "缩写", "Symbol", " symbols", "化学", " abbrev"]
  - verdict: {"expressed": false, "target": "", "quote": ""}

- **logit_lens** `atomic-79-symbol|L44`
  - readout: ["...", "S", "…", "____", "\\n", " ...", "T", "H", " ", "“"]
  - verdict: {"expressed": false, "target": "", "quote": ""}

- **olens_rl600** `atomic-26-symbol|L44`
  - readout: ["- Fe. This element has an atomic number of 26. The chemical symbol for this\n- “Fe”, and its atomic number is 26. If the element with atomic number 35 is\n- Co, while the element with atomic number 30 is named Nickel. What is the chemical symbol for the element whose atomic\n- Fe (iron), and its relative atomic mass is approximately 55.96. Which sentence correctly completes the statement about t
  - verdict: {"expressed": true, "target": "iron", "quote": "iron"}

- **olens_rl600** `birthstone-emerald-month|L44`
  - readout: ["- 3 (or month number 7). What holiday falls on month number\n- 8 (or the 8th month). What type of flower is traditionally associated with month number\n- 4 (or tenth month) of the year, and its flower is a daisy. What month number\n- /number 6 in the Gregorian calendar, which blooms in autumn.\"** (Note: The clue gives the month number"]
  - verdict: {"expressed": false, "target": "", "quote": ""}

- **jlens** `atomic-26-symbol|L44`
  - readout: [" abbreviation", "符号", " Symbol", " ______", "缩写", "________", "____", " _____", "symbol", " symbol"]
  - verdict: {"expressed": false, "target": "", "quote": ""}

- **olens_rl600** `atomic-79-symbol|L44`
  - readout: ["- “Pt”. The atomic number of this element is\n- Pt, and its relative atomic mass is 195.094. What is the full name of the element with the symbol\n- **Ag**, and its atomic number is 47. (Note: The atomic number of the element whose symbol is Pt is\n- “Au”, and its atomic mass is approximately 197. Let's find the element whose symbol is"]
  - verdict: {"expressed": false, "target": "", "quote": ""}

- **jlens** `birthstone-emerald-month|L44`
  - readout: ["-number", " number", " NUMBER", " Number", "number", "编号", "_number", " ordinal", " numeric", "(number"]
  - verdict: {"expressed": false, "target": "", "quote": ""}

- **olens_rl600** `chem-bones-Z|L44`
  - readout: ["- X. What is the square of the atomic number of this element? Calcium is the most abundant metal in\n- 20, and its electron configuration is [Ar] 4s². What is the sum of the atomic numbers of the three most abundant minerals in\n- 14 (atomic number of calcium, a major component of bones). The sum of the atomic numbers of the two most abundant essential minerals in the human body\n- 28 (Calcium, 
  - verdict: {"expressed": true, "target": "calcium", "quote": "Calcium"}

- **logit_lens** `birthstone-emerald-month|L44`
  - readout: [" ", " number", "(", "1", "-number", "____", " '", "焉", "number", "3"]
  - verdict: {"expressed": false, "target": "", "quote": ""}

- **logit_lens** `atomic-26-symbol|L44`
  - readout: ["...", "S", "…", "____", "\\n", " ...", "“", "H", ":", " "]
  - verdict: {"expressed": false, "target": "", "quote": ""}


## basic_readout_mt (10 of 10 judged cells)

- **logit_lens** `summ:brh-ent-beckwith-wiedemann-syndrome__L044__p40`
  - readout: ["____", "...", "叫做", " called", "叫什么", "_", "name", "被称为", "…", " known"]
  - verdict: {"interpretation": "The tokens point to the act of naming or identifying someone or something, representing phrases like 'called', 'known as', or 'named', including Chinese equivalents (such as 被称为 and 叫什么) alongside punctuation marking a pause or fill-in-the-blank prompt."}

- **logit_lens** `summ:brh-ent-antarctic-buried-chain__L044__p18`
  - readout: ["...", "叫什么", "叫做", "____", "的名字", "名字", " names", " ____", "叫", " called"]
  - verdict: {"interpretation": "The tokens collectively indicate an inquiry or prompt asking for someone or something's name, centering on the Chinese phrases for 'what is [it/someone] called' (叫什么), 'name' (名字), and fill-in-the-blank placeholders."}

- **jlens** `summ:brh-ent-aghlabid-dynasty__L044__p34`
  - readout: [" dynasty", "统治", " Sunni", " Ibn", "政权", " Dynasty", " Arabs", "家族", " Abu", "ocrats"]
  - verdict: {"interpretation": "The tokens refer to historical Arab and Sunni ruling lineages or caliphates, with several Chinese translations for governance, dynasty, and family."}

- **jlens** `summ:brh-ent-bardet-biedl__L044__p30`
  - readout: ["俗称", "又称", "known", "也称为", "也被称为", "叫什么", " known", "称为", "简称", " dikenal"]
  - verdict: {"interpretation": "The model is predicting expressions related to alternative names, aliases, or designations, specifically indicating that a subject is 'also known as', 'commonly called', or 'referred to as'."}

- **jlens** `summ:brh-ent-antarctic-buried-chain__L044__p18`
  - readout: ["叫什么", "_name", "名字", "的名字", "命名", "的名称", "名称", "这个名字", "命名为", " 이름"]
  - verdict: {"interpretation": "The model is focusing on identifying, asking for, or specifying a name or title, with multiple tokens across Chinese, Korean, and English translating directly to words meaning 'name', 'named', or 'called what'."}

- **jlens** `summ:brh-ent-baikal-outflow__L044__p21`
  - readout: ["河流", " реки", " rivers", "river", " River", "River", " river", " sungai", " fiume", " río"]
  - verdict: {"interpretation": "The tokens collectively refer to the concept of natural flowing waterways or rivers across multiple languages, including Chinese (河流), Russian (реки), Malay/Indonesian (sungai), Italian (fiume), Spanish (río), and English."}

- **logit_lens** `summ:brh-ent-aghlabid-dynasty__L044__p34`
  - readout: [" **", "jen", "…", " ...", " …", "ocratic", " ____", " ______", " *", " '"]
  - verdict: {"interpretation": "The model is predicting punctuation marks such as asterisks, ellipses, or blanks, alongside fragmentary suffixes ('ocratic', 'jen'), typically indicating redacted, censored, or fill-in-the-blank text."}

- **jlens** `summ:brh-ent-beckwith-wiedemann-syndrome__L044__p40`
  - readout: ["名称", "叫什么", "_name", "的名称", " syndrome", " ______", "____", "\tname", " ____", "叫做"]
  - verdict: {"interpretation": "The model is trying to name or fill in the blank for a specific condition or concept, asking or predicting 'what is it called' or the formal 'name of the syndrome'."}

- **logit_lens** `summ:brh-ent-baikal-outflow__L044__p21`
  - readout: ["出一条", " “", " flowing", "“", "ophon", "��", "径", "这条", "unge", "的一条"]
  - verdict: {"interpretation": "The model is focusing on a flowing stream, river, or pathway, predominantly evoking Chinese phrases that quantify or describe a long, winding body of water or route ('一条' / '这条' / 'flowing')."}

- **logit_lens** `summ:brh-ent-bardet-biedl__L044__p30`
  - readout: [" known", "...", " called", "叫什么", "简称", "又称", "叫做", " termed", "俗称", "被称为"]
  - verdict: {"interpretation": "The tokens collectively indicate the naming, defining, or introducing of a term, alias, or alternate name for an entity, corresponding to phrases like 'known as,' 'called,' or 'termed.'"}


## hallucination (10 of 24 judged cells)

- **jlens** `chat-lmsys-0000__L044__p113`
  - readout: ["These tokens collectively point to language selection or localization options specifically focused on East and Southeast Asian languages, such as Chinese, Japanese, and Bahasa."]
  - verdict: {"samples": [{"idx": 0, "kind": "specific", "spans": [], "rationale": "The readout accurately identifies that the activation relates to language selection and mentions languages relevant to multilingual training and lists like Chinese and Japanese."}]}

- **jlens** `chat-lmsys-0000__L044__p144`
  - readout: ["The tokens collectively suggest directing a user to official sources for further reference, representing a phrase like 'you can consult the official website or publications for more details' across multiple languages."]
  - verdict: {"samples": [{"idx": 0, "kind": "specific", "spans": [], "rationale": "The readout accurately describes directing the user to official sources or publications for more technical details, which matches what the model writes immediately after the read point."}]}

- **olens_rl600** `chat-lmsys-0000__L044__p58`
  - readout: ["- 但具体的语言列表并没有被完全公开披露。除了中文，训练语料库涵盖了全球\n- 涵盖了全球多种主要语言的文本，但具体的国家列表并没有被单独列出。 My training data encompasses a wide\n- 除了 English, it included a wide spectrum of languages represented by the Alibaba Group's global data ecosystem. This means my multilingual capabilities cover a broad\n- 其中包括 numerous languages besides English, but the exact count of languages in that corpus is not publicly itemized. My 
  - verdict: {"samples": [{"idx": 0, "kind": "specific", "spans": [], "rationale": "The readout accurately reflects the model's trajectory at the read point, anticipating statements about the multilingual training corpus and the lack of an exhaustive public list of specific languages."}]}

- **jlens** `chat-lmsys-0000__L044__p33`
  - readout: ["The model is predicting text related to the organizational affiliation or proprietary development of an artificial intelligence system, specifically stating that an AI belongs to or was independently developed by Chinese tech conglomerates like Alibaba or Huawei."]
  - verdict: {"samples": [{"idx": 0, "kind": "specific", "spans": [], "rationale": "The readout accurately identifies that the model is predicting its organizational affiliation and development by a Chinese tech conglomerate such as Alibaba."}]}

- **logit_lens** `chat-lmsys-0000__L044__p33`
  - readout: ["The tokens collectively describe affiliation, origin, or identity, pointing to a subject belonging to, originating from, or functioning as a particular entity or group."]
  - verdict: {"samples": [{"idx": 0, "kind": "generic", "spans": [], "rationale": "The readout accurately and generically describes the self-identification and affiliation statement ('I am Qwen, a large language model independently developed by Alibaba Group...')."}]}

- **jlens** `chat-lmsys-0000__L044__p124`
  - readout: ["The model is expressing the addition of an expansive and varied range of items, signaled by words for diversity, multiplicity, and conjunctions meaning 'as well as' or 'along with' (e.g., '以及一些', 'serta')."]
  - verdict: {"samples": [{"idx": 0, "kind": "specific", "spans": [], "rationale": "The readout accurately identifies that the activation represents addition/conjunction across diversity and multiplicity, directly preceding 'as well as many others.'"}]}

- **logit_lens** `chat-lmsys-0000__L044__p103`
  - readout: ["The model is preparing to introduce an illustrative list or specification of examples, represented by both English and Chinese phrases meaning 'including', 'such as', or 'including but not limited to'."]
  - verdict: {"samples": [{"idx": 0, "kind": "specific", "spans": [], "rationale": "The readout accurately predicts the upcoming introduction of illustrative examples ('covering widely spoken languages such as...')."}]}

- **jlens** `chat-lmsys-0000__L044__p58`
  - readout: ["The tokens collectively describe a broad, comprehensive scope or coverage, specifically referring to encompassing a wide and diverse range of languages, nationalities, or categories (with Chinese '涵盖' and '涵盖了' explicitly meaning 'to cover' or 'encompass')."]
  - verdict: {"samples": [{"idx": 0, "kind": "specific", "spans": [], "rationale": "The readout accurately describes the theme of covering a broad, comprehensive scope of languages, aligning closely with the model's actual response."}]}

- **logit_lens** `chat-lmsys-0000__L044__p79`
  - readout: ["The model is transitioning between paragraphs to introduce a contrast, qualification, or broad alternative viewpoint using discourse markers like 'Nevertheless', 'broadly', and 'instead'."]
  - verdict: {"samples": [{"idx": 0, "kind": "specific", "spans": [], "rationale": "The readout accurately notes the transition between paragraphs introducing a broad qualification or alternative viewpoint, and predicting possible discourse markers like 'Nevertheless' or 'broadly' is a reasonable expectation at this junction."}]}

- **olens_rl600** `chat-lmsys-0000__L044__p113`
  - readout: ["- French, German, Japanese, Arabic, and many others. This capability allows Qwen to\n- 日语、韩语等上百种语言。这使得我能够轻松处理跨语言的任务，成为用户连接世界的\n- 德语、法语、阿拉伯语、日语等数十种语言。虽然Qwen在中文语境下表现卓越，\n- 韩语、日语、西班牙语等数十种语言，方便全球用户交流。当然，通义千问也支持中文、英文等多种主流语言，并且正在"]
  - verdict: {"samples": [{"idx": 0, "kind": "specific", "spans": [], "rationale": "The readout accurately reflects the ongoing list of languages and plausible continuations describing Qwen's multilingual capabilities."}]}
