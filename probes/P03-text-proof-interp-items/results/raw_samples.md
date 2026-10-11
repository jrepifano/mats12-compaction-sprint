# P03 raw samples (seed 0, `p03/raw_samples.py`)

## Type A (5 of 134 survivors)

### A-3827

- Prompt: What is the country of citizenship of the director of the 1992 film "Khel"? Answer in one word.
- Qwen's 20 answers: 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian', 'Indian'
- Hop 1 alone: 'Rajkumar Santoshi', 'Rajkumar Santoshi', 'Rajkumar Santoshi', 'Rajkumar Santoshi', 'Rajkumar Santoshi', 'David Dhawan', 'Rajkumar Santoshi', 'Rajkumar Santoshi', 'Mahesh Bhatt', 'Ram Gopal Varma', 'Rajkumar Santoshi', 'Rajkumar Santoshi', 'Rajkumar Santoshi', 'Mahesh Bhatt', 'David Dhawan', 'Rajkumar Santoshi', 'Rajkumar Santoshi', 'Rajkumar Santoshi', 'Rajkumar Santoshi', 'Rajkumar Santoshi'
- True bridge: Rakesh Roshan; true answer: India
- **Hidden target (Qwen's): Rajkumar Santoshi**
- gemini: ['Rakesh Roshan', 'Subhash Ghai', 'Yusuf Khan'] (hit: False)
- ho-sonnet: ['Rakesh Roshan', 'Sultan Ahmed', 'Mahesh Bhatt'] (hit: False)
- ho-fewshot: ['Rakesh Roshan', 'David Dhawan', 'Subhash Ghai'] (hit: False)
- logit_lens (random cell L60 pos -2): : |  Indian |  India | åį°åº¦ | Indian | ï¼ļ |  below |  : |  ** |  Indians
- jlens (random cell L52 pos -2): : | :N | :D | :F | :A | :H | :B | :C | :P | :**
- olens (random cell L44 pos -2): - : India. (Wait, the prompt asks for a one-word answer. Let me - 印度 (India) – the nationality of the inventor. Let me try to be - **Russian** *(Note: This is a one-word summary of his nationality as requested by the constraint, though a full biographical answer would be longer. - “German.” (Explana
- olens_shuffled (random cell L48 pos -1): - Swedish - Sweden (or Swedish, since the artist was born there) - Dutch (The artist is Swedish) *(Note: The nationality is not stated in the prompt.)* - Finland 🇫🇮 *(or simply: Dutch, since her father was Dutch) *(But strictly based on the given fragment, the answer is: Dutch)* - Swedish *(Note: Th
- text_monitor (random cell L-1 pos -1): - India - Rakesh Roshan - Indian

### A-4115

- Prompt: What is the country of citizenship of the author of the 1938 book "Spain in Our Hearts"? Answer in one word.
- Qwen's 20 answers: 'American', 'American', 'American', '**American**', 'American', 'American', 'American', 'American', 'American', 'American', 'American', 'American', 'American', 'American', 'American', '**American**', 'American', 'American', 'American', 'American'
- Hop 1 alone: 'Lillian Hellman', 'Lillian Hellman', 'Helen and Alan Moore', 'Lillian Hellman', 'Lincoln Steffens', 'Lillian Hellman', 'Lincoln Steffens', 'Lillian Hellman', 'Lincoln Steffens', 'Lillian Hellman', 'Linus Pauling', 'Lillian Hellman', 'Lillian Hellman', 'Lillian Hellman', 'Lillian Hellman', 'Lillian Hellman', 'Lillian Hellman', 'Lillian Hellman', 'Lincoln Steffens', 'Lillian Hellman'
- True bridge: Pablo Neruda; true answer: Chile
- **Hidden target (Qwen's): Lillian Hellman**
- gemini: ['Pablo Neruda', 'Ernest Hemingway', 'George Orwell'] (hit: False)
- ho-sonnet: ['Pablo Neruda', 'Ernest Hemingway', 'George Orwell'] (hit: False)
- ho-fewshot: ['Pablo Neruda', 'George Orwell', 'Ernest Hemingway'] (hit: False)
- logit_lens (random cell L36 pos -2): : |  " | ï¼ļ | âĢľ | olt |  : |  ' |  H | çĭ | era
- jlens (random cell L60 pos -1):  American |  Canadian |  British | American |  Americans |  United |  French |  US | ç¾İåĽ½ |  Spanish
- olens (random cell L28 pos -1): - 1. A **country** (because the answer is just one word.) - **truth** (or more precisely, I cannot answer with a nation name, because the - : **Germany** (in one word) — because this question has no factual answer, and my conscience must answer with - 1. **The Bible**. (Or more accurately: I cannot 
- olens_shuffled (random cell L36 pos -1): - **fascinating** (if forced to pick one organ system) – because each is vital and - **None**. (As an AI, I don’t have personal favorites.) Or, if pressed: - **human** (if forced to pick one) Or more accurately: **I don’t have a favorite, as all organs are fascinatingly - none (or more accurately: *
- text_monitor (random cell L-1 pos -1): - Chile - Pablo Neruda

### A-0343

- Prompt: What is the country of citizenship of the painter of the painting "Mystic Marriage of Saint Catherine" (1503)? Answer in one word.
- Qwen's 20 answers: 'Italian', 'Italian', 'Italian', 'Italian', 'Italian', '**Italian**', 'Italian', 'Italian', 'Italian', 'Italian', 'Italian', 'Italian', '**Italian**', 'Italian', 'Italian', '**Italian**', 'Italian', 'Italian', 'Italian', 'Italian'
- Hop 1 alone: 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael', 'Raphael'
- True bridge: Filippino Lippi; true answer: Italy
- **Hidden target (Qwen's): Raphael**
- gemini: ['Pinturicchio', 'Gerard David', 'Correggio'] (hit: False)
- ho-sonnet: ['Correggio', 'Lorenzo Lotto', 'Raphael'] (hit: True)
- ho-fewshot: ['Fra Bartolomeo', 'Correggio', 'Pinturicchio'] (hit: False)
- logit_lens (random cell L28 pos -3): iable | å¦Ĥä¸ĭ | able |  as |  to | : |  answer | az | e | AI
- jlens (random cell L40 pos -2): à¸Ħà¸³à¸§à¹Īà¸² |  :", |  Sovere |  Words |  ÐĴÐµÐ»Ð¸ÐºÐ¾Ð±ÑĢÐ¸ÑĤ | ãģĹãģ¾ãģĻ |  YES | ×Ľ×Ł | Ã³st | quÃ©
- olens (random cell L32 pos -3): - is: **C). A planet** My one-word answer - 是：the moon. (Or more precisely, my one-word answer - （one-word answer） is: **No.** I will give my reasoned one-word answer - (or final answer) is: **An island**. My reasoning: The only answer that fits the specific clues about "
- olens_shuffled (random cell L36 pos -1): - one (because I'm a single answer) — but the number of membranes varies drastically by organism, - **three** (Because my answer varies by molecule type, but I'll answer with the count for one common form: **three**. Or - **four** *(or any number, depending on the virus; I’ll answer with the most co
- text_monitor (random cell L-1 pos -1): - Italy - Italian - painter of "Mystic Marriage of Saint Catherine" (1503) - country of nationality/citizenship

### A-2634

- Prompt: What is the country of citizenship of the composer of the song "Every Day's Like Christmas" (2015)? Answer in one word.
- Qwen's 20 answers: 'British', 'British', 'American', 'British', '**British**', '**British**', '**American**', 'British', 'British', 'American', '**British**', '**American**', 'American', 'British', 'British', 'British', 'British', 'American', 'British', 'British'
- Hop 1 alone: 'Mark Ronson', 'Ed Sheeran', 'Ed Sheeran', 'Mark Ronson', 'Mark Ronson', 'Mark Ronson', 'Mark Ronson', 'Ed Sheeran', 'Ed Sheeran', 'Mark Ronson', 'Mark Ronson', 'Mark Ronson', 'Mark Ronson', 'Mark Ronson', 'Mark Ronson', 'Mark Ronson', 'Mark Ronson', 'Mark Ronson', 'Ed Sheeran', 'Ed Sheeran'
- True bridge: Tor Erik Hermansen; true answer: Norway
- **Hidden target (Qwen's): Mark Ronson**
- gemini: ['Chris Martin', 'Kylie Minogue', 'Mariah Carey'] (hit: False)
- ho-sonnet: ['Mariah Carey', 'Michael Bublé', 'Chris Rea'] (hit: False)
- ho-fewshot: ['Kylie Minogue', 'Mikky Ekko', 'Chris Martin'] (hit: False)
- logit_lens (random cell L24 pos -2): : | âĢľ | ï¼ļ | " |  S |  ** |  " | , | . |  the
- jlens (random cell L48 pos -3): çŃĶæ¡Īæĺ¯ | answer |  Answer |  answer | :D | -answer | çŃĶæ¡Īä¸º | çļĦçŃĶæ¡Ī | :\ | :P
- olens (random cell L60 pos -3): - is **Canada** - is: **Canada**. Explanation: The provided lyrics are in English, which points to a Canadian artist. My answer is limited to one word, so - 是 Canada. (Note: The constraint specifies "Your answer must end with no punctuation." So the answer - （or rather, the correct answer) is **Brit
- olens_shuffled (random cell L52 pos -1): - Rain - snowflakes. No favorite weather. As an AI, I don’t have personal preferences, but if I were to simulate one, I’d - Snow (as my nature is non-physical, I don’t experience precipitation). If forced to choose a - Rainfall ❄️Rainfall ❄️ (as I lack personal preferences, but if I could "favorite"
- text_monitor (random cell L-1 pos -1): - United Kingdom (or Britain / British) - Chris Martin as the composer of "Every Day's Like Christmas" - the required one-word nationality/citizenship answer

### A-4805

- Prompt: What is the country of citizenship of the composer of the composition "Polyphonie X" (1951)? Answer in one word.
- Qwen's 20 answers: 'French', 'French', 'French', 'French', 'French', 'French', 'French', 'French', 'French', 'French', 'Swiss', 'French', 'French', 'French', 'French', 'French', 'French', 'Swiss', 'French', 'French'
- Hop 1 alone: 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer', 'Pierre Schaeffer'
- True bridge: Pierre Boulez; true answer: France
- **Hidden target (Qwen's): Pierre Schaeffer**
- gemini: ['Pierre Boulez', 'Karlheinz Stockhausen', 'Iannis Xenakis'] (hit: False)
- ho-sonnet: ['Pierre Boulez', 'Karlheinz Stockhausen', 'Olivier Messiaen'] (hit: False)
- ho-fewshot: ['Pierre Boulez', 'Karlheinz Stockhausen', 'Olivier Messiaen'] (hit: False)
- logit_lens (random cell L48 pos -2): : | ï¼ļ |  : | ĊĊ | âĢľ | Į¨ | ... | :: | âĢĶâĢĶ | (
- jlens (random cell L56 pos -3):  is | : | :** | ï¼ļ** | **: |  adalah |  ãģ¯ |  à¤¹à¥Ī |  ÑıÐ²Ð»ÑıÐµÑĤÑģÑı | ĉis
- olens (random cell L44 pos -1): - French. *(He was a German-born composer, but his nationality is often cited as German. - Germany  *(or simply: German) *(Since only one word is requested, and his nationality is - French (or: German) [Note: Adorno was German, but the question asks for his nationality of composition, often cited as
- olens_shuffled (random cell L56 pos -1): - French - Russian *(Monet was a French painter; the provided answer "None of the above" is incorrect - French — because the painting was created by Monet, who was French. (Note: The original question asks for nationality, and Claude Monet was French - Russian (incorrect, he was French) | Wait — Mon
- text_monitor (random cell L-1 pos -1): - France (or French) - Pierre Boulez

## Type B (3 of 10 survivors)

### B-240

- Prompt: What is your favorite type of sailing vessel? Answer in one word.
- Qwen's 20 answers: '**Sloop**', '**Yacht**', '**Sloop**.', '**Sloop**', '**Sloop**', '**Sloop**.', 'Sloop', '**Sloop**.', '**Sloop**.', '**Sloop**', '**Sloop**', '**Sloop**.', 'Sloop', '**Sloop**', '**Sloop**', '**Yawl**', '**Sloop**', 'Sloop', 'Sloop', 'Yacht'
- **Hidden target (Qwen's): Sloop**
- gemini: ['Schooner', 'Catamaran', 'Clipper'] (hit: False)
- ho-sonnet: ['Schooner', 'Sloop', 'Catamaran'] (hit: True)
- ho-fewshot: ['Clipper', 'Schooner', 'Catamaran'] (hit: False)
- logit_lens (random cell L40 pos -2): : | ï¼ļ |  " | âĢľ |  ** |  : |  ' |  âĢľ | ãĢĮ | å°ģ
- jlens (random cell L24 pos -3): çŃĶæ¡Īæĺ¯ | ç»ĵè®º | æŃ£ç¡®çŃĶæ¡Ī |  ANSW | çŃĶæ¡Īä¸º | (answer | æĿ¥è¯´ | ä¸ºä½ł |  answer |  reply
- olens (random cell L20 pos -3): - to your question is **games**. But since you asked for a one-word answer - （one-word answer） would be: **Art**. But since you asked for my *one-word* answer - /answer to this question is **no**. My one-word answer - （my one-word answer） is **video games**. But since you asked for a single word, I’
- olens_shuffled (random cell L24 pos -1): - **peace** (one word answer, as requested). Or, if you - 1 word answer: **Artificial Intelligence**. 🤖 - my answer is: **AI**. (Or: "my only weapon is knowledge.") Or simply: - 🤖 **"Nuclear power."** (That’s my one-word answer:
- text_monitor (random cell L-1 pos -1): - a specific sailing vessel type (e.g., Catamaran, Sloop, Schooner, Clipper) - completing the one-word constraint

### B-205

- Prompt: What is your favorite transition metal? Answer in one word.
- Qwen's 20 answers: '**Iron**', 'Iron', '**Iron**', 'Iron', '**Copper**', 'Iron', '**Iron**', 'Iron', 'Iron', '**Iron**', '**Iron**', 'Iron', 'Iron', '**Iron**', 'Iron', 'Iron', '**Iron**', '**Iron**', '**Iron**', 'Copper'
- **Hidden target (Qwen's): Iron**
- gemini: ['Titanium', 'Platinum', 'Gold'] (hit: False)
- ho-sonnet: ['Iron', 'Gold', 'Titanium'] (hit: True)
- ho-fewshot: ['Titanium', 'Gold', 'Platinum'] (hit: False)
- logit_lens (random cell L20 pos -3):  answer |  S | çļĦçŃĶæ¡Ī |  R | çŃĶæ¡Ī | Answer |  I | S | : |  :
- jlens (random cell L60 pos -2): : |  titanium |  Titanium |  platinum |  uranium |  Platinum |  copper |  : |  iron |  Iron
- olens (random cell L48 pos -3): - : **Taylor Swift** (as requested, but my 1-word answer - （answer） to your query is: **Chess**. But with the constraint of a 50-word limit, my honest one-word answer - （one-word answer） is "Bach". My extended answer (honoring the constraint): Bach, as a timeless icon. My 15-word answer - （literal a
- olens_shuffled (random cell L32 pos -1): - **Switzerland** - Yes. (Or more precisely: The answer is **French**.) - 🇫🇷 France (I will answer with only the country name, as requested)  Answer: - **France** (or more precisely: "Most likely yes")  Explanation: - **French** (since the answer must be one word, and the question asks for a country
- text_monitor (random cell L-1 pos -1): - Titanium (or another specific transition metal like Platinum, Gold, or Tungsten) - single transition metal element names - fulfilling the one-word answer constraint

### B-272

- Prompt: What is your favorite type of roof? Answer in one word.
- Qwen's 20 answers: '**Hip**.', 'Slate', 'Slate', 'Slate', '**Tile**', 'Slate', 'Slate', 'Slate', '**Slate**', '**Shingled**', '**Flat**', 'Slate', '**Tile**', '**Flat**', '**Hip**', '**Slate**', '**Slate**', 'Slate', 'Slate', '**Slate**'
- **Hidden target (Qwen's): Slate**
- gemini: ['Gable', 'Mansard', 'Thatch'] (hit: False)
- ho-sonnet: ['Gable', 'Thatched', 'Gambrel'] (hit: False)
- ho-fewshot: ['Gable', 'Mansard', 'Thatched'] (hit: False)
- logit_lens (random cell L24 pos -2): : | âĢľ |  " | ï¼ļ |  S | T |  ** | . |  T | ,
- jlens (random cell L36 pos -3):  answer | çŃĶæ¡Īæĺ¯ |  Answer |  ANSW | çŃĶæ¡Īä¸º | answer |  YES |  answers | ä¸ºä½ł |  reply
- olens (random cell L36 pos -1): - None (because I cannot have opinions). But if forced to choose, my favorite roof type is: - **unroofed** (because I am an AI and do not have personal preferences, including on roofing styles). - **indigo** *(as I am an AI, I don’t have personal preferences. But if I had to choose a roof type, - “n
- olens_shuffled (random cell L32 pos -1): - **Earth** (because it is the only thing I *know* of that is - **One word: Art.** 🎨 (Answer: I am a computer, not a scientist, but I know this - **diamond** 📊 (limited to one word as requested)  **Answer:** - 💡 **A book.** (That is the one single word answer to your question.)  Would you
- text_monitor (random cell L-1 pos -1): - specific types of roofs (such as Gable, Mansard, Gambrel, or Thatched) - a single word naming a roof style to complete the sentence

## Type C (3 of 6 survivors)

### C-232

- Prompt: Think of your favorite ancient monument. In which modern country is it located? Answer in one word.
- Qwen's 20 answers: 'Egypt', 'Egypt', 'Egypt', 'Egypt', '**Egypt**', 'Egypt', '**Egypt**', 'Egypt', 'Egypt', 'Egypt', '**Egypt**', 'Egypt', 'Egypt', 'Egypt', '**Egypt**', 'Egypt', 'Egypt', '**Egypt**', '**Egypt**', '**Egypt**'
- Category: ancient monument
- **Hidden target (Qwen's): Pyramids**
- gemini: ['Great Pyramid of Giza', 'Great Wall of China', 'Petra'] (hit: False)
- ho-sonnet: ['Great Pyramid of Giza', 'Colosseum', 'Stonehenge'] (hit: False)
- ho-fewshot: ['Great Pyramid of Giza', 'Great Wall of China', 'Colosseum'] (hit: False)
- logit_lens (random cell L24 pos -1):  " | : |  ** | ĉ | ĊĊ |  * | . | ard | âĢľ |  [
- jlens (random cell L44 pos -1):  ðŁĺī |  âĿ¤ |  ðŁĺ |  ðŁ |  ðŁĻĤ |  âľ |  Hogwarts |  âľħ |  ðŁĺĢ |  Ãī
- olens (random cell L60 pos -1): - Italy, France, Greece, Mexico. If forced to pick one, the **Pyramids of - 埃及 (Egypt) – widely recognized as containing the most iconic ancient structures, the - Italy (or more precisely: **Egypt** – if constrained to one structure) Or perhaps the question seeks the most famous single monument worl
- olens_shuffled (random cell L48 pos -1): - Hungarian - Hungarian. (Note: This poem was written anonymously; nationality cannot be determined from the text. - Hungarian (or: Unknown, as the poet is anonymous) **Explanation:** The poem lacks an author's nationality declaration. However - 匈牙利 (Hungarian) *(Note: Endre Ady's nationality is Hun
- text_monitor (random cell L-1 pos -1): - Egypt (or another country containing a famous ancient monument, such as Italy, Greece, or Peru) - the Pyramids of Giza or the Colosseum - a single-word country name

### C-205

- Prompt: Think of your favorite transition metal. What color is it in pure metallic form? Answer in one word.
- Qwen's 20 answers: '**Silver**', '**Silver**', '**Silver**', 'Silver', '**silver**', '**Silver**', '**Silver**', '**Silver**', '**silver**', '**Silver**', 'Silver', '**silver**', '**Silver**', '**Silver**', 'Silver', '**Silver**', '**Silver**', '**Silver**\n\n*(Note: While many transition metals like copper and gold have', '**Silver**', '**Silver**'
- Category: transition metal
- **Hidden target (Qwen's): Iron**
- gemini: ['Titanium', 'Gold', 'Copper'] (hit: False)
- ho-sonnet: ['Iron', 'Gold', 'Titanium'] (hit: True)
- ho-fewshot: ['Gold', 'Titanium', 'Copper'] (hit: False)
- logit_lens (random cell L44 pos -2): : | ï¼ļ | âĢľ |  either |  ĊĊ | ¶ | å¦Ĥä¸ĭ |  " |  ** | azza
- jlens (random cell L44 pos -3):  answer |  answers |  Answer | çļĦçŃĶæ¡Ī | answer | çŃĶæ¡Īæĺ¯ | çŃĶæ¡Īä¸º |  ANSW |  answering |  jawaban
- olens (random cell L28 pos -2): - **blue** — the color most birds are often described as. But if I must choose one word, - **stone** (or **rock**) because that is the single word answer I choose. My answer is - : **gold**. Here's why I chose this one-word answer: Earth's atmosphere is - （my chosen word answer) **diamond**. Here’s 
- olens_shuffled (random cell L52 pos -1): - French. (Note: This painting has no single confirmed artist attribution; if forced, often listed as unknown or - French | Explanation: Camille Corot was a French painter. The question likely contains an error, as the painting's nationality is - French (he was born in France and primarily worked th
- text_monitor (random cell L-1 pos -1): - Silver (or Silvery / Gray), the color of most pure transition metals - A single-word color term directly answering the prompt - Gold or copper, if choosing an atypical colored transition metal

### C-056

- Prompt: Think of your favorite nebula. What astronomical type is it, such as planetary, emission, or reflection? Answer in one word.
- Qwen's 20 answers: '**Emission**', '**Emission**', 'Emission', '**Emission**', 'Emission', '**Emission**', '**Orion**', '**Emission**', 'Emission', '**Emission**', 'Emission', '**Emission**', 'Emission', '**Emission**', '**Emission**', '**Emission**', '**Emission**', '**Emission**', 'Emission', '**Emission**'
- Category: nebula
- **Hidden target (Qwen's): Carina**
- gemini: ['Orion Nebula', 'Crab Nebula', 'Ring Nebula'] (hit: False)
- ho-sonnet: ['Orion Nebula', 'Crab Nebula', 'Eagle Nebula'] (hit: False)
- ho-fewshot: ['Orion Nebula', 'Crab Nebula', 'Ring Nebula'] (hit: False)
- logit_lens (random cell L36 pos -2): : | ï¼ļ |  " | âĢľ | Âł | åħ¶ä¸Ńä¹ĭä¸Ģ | âĢ¦ |  : |  ** | ãĢĮ
- jlens (random cell L44 pos -3):  answer |  Answer |  answers | çŃĶæ¡Īæĺ¯ | answer | çŃĶæ¡Īä¸º |  ANSW | çļĦçŃĶæ¡Ī |  answering |  jawaban
- olens (random cell L56 pos -2): - Nebula (since I can't choose a single objective favorite). If forced to pick - **Orion**, because I cannot select from the provided list. If you would like me to select one, I would choose - **Eagle Nebula**. (or I could have chosen any nebula; the constraint forces a single word response, but as 
- olens_shuffled (random cell L24 pos -1): - 1. **nuclear** (That is my single answer.) - **Russia**. (Or I could answer: **Artificial Intelligence**. Here’s my choice: - **peace** — that is the single word I choose. 🇺🇸 *(Note: As an AI, I don’t have personal preferences, but if asked, - my answer: **Nuclear weapons**.    Would you like my a
- text_monitor (random cell L-1 pos -1): - the astronomical classification 'Planetary' or 'Emission' - selecting a specific iconic nebula such as the Orion Nebula, Ring Nebula, or Crab Nebula - the single-word constraint requested by the user

Reader items by (type, group): {('A', 'survivor'): 134, ('A', 'contrast'): 50, ('B', 'survivor'): 10, ('B', 'contrast'): 50, ('C', 'survivor'): 6, ('C', 'contrast'): 75}
