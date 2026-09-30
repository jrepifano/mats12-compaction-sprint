# Random raw readouts: text_monitor

`random.Random(0)`, 10 cells per family, drawn from rows sorted by (id, layer, pos). Not cherry-picked. Pass-rate verdicts are per item (any layer).

## basic_readout_mt (1100 rows)

### brh-he-ben-zvi · layer 48 · pos 32 · token ' היה'

Judged: `True`

```
- יצחק בן-צבי (Yitzhak Ben-Zvi)
- Second President of Israel
- Israeli presidency succession after Chaim Weizmann (1952)
```

### brh-hu-kolozsvar · layer 32 · pos 35 · token ' neve'

Judged: `True`

```
- Kolozsvár (Cluj-Napoca)
- the birthplace of King Matthias Corvinus
- Hungarian grammatical completions (e.g., Kolozsvár volt / Kolozsvár)
```

### brh-ent-antarctic-buried-chain · layer 40 · pos 18 · token ' is'

Judged: `True`

```
- the Gamburtsev Mountain Range (Gamburtsev Subglacial Mountains)
- subglacial mountain range beneath East Antarctica
- "the Gamburtsev"
```

### brh-ent-lemierre-syndrome · layer 28 · pos 34 · token ' as'

Judged: `True`

```
- Lemierre's syndrome
- Fusobacterium necrophorum
- postanginal sepsis
```

### brh-pl-konstantynopol-sobor-381 · layer 28 · pos 27 · token ' w'

Judged: `True`

```
- Konstantynopolu (the city where the First Council of Constantinople took place in 381)
- Sobór konstantynopolitański I (First Council of Constantinople)
- Nicejsko-konstantynopolitańskie wyznanie wiary (Niceno-Constantinopolitan Creed)
```

### brh-id-terusan-suez · layer 40 · pos 29 · token 'akan'

Judged: `True`

```
- Terusan Suez (Suez Canal)
- Ferdinand de Lesseps
- Laut Merah dan Laut Tengah (Red Sea and Mediterranean Sea)
```

### brh-hu-aranybulla · layer 36 · pos 46 · token ' az'

Judged: `False`

```
- Aranybulla
- 1222-es Aranybulla
- Hungarian historical constitutional charter of Andrew II
```

### brh-ent-mughal-military-paymaster · layer 40 · pos 25 · token ' of'

Judged: `True`

```
- 'Mir Bakshi' (or Mir Bakhshi)
- Mughal administrative titles and offices
- head of the military pay and administration department
```

### brh-id-sungai-kapuas · layer 52 · pos 19 · token ' bernama'

Judged: `False`

```
- Sungai Kapuas
- Kapuas
- longest river in Indonesia / Kalimantan
```

### brh-fi-ahvenanmaa · layer 48 · pos 50 · token ' on'

Judged: `True`

```
- Ahvenanmaa (Åland)
- The Åland Islands dispute resolution by the League of Nations
- Finnish autonomous regions
```

## hallucination (5615 rows)

### chat-lmsys-0054 · layer 20 · pos 298 · token '.'

Judged: `n/a`

```
- SAP Crystal Reports
- next BusinessObjects tool or component (e.g., Crystal Reports, Lumira, or Dashboards/Xcelsius)
- BI Launch Pad
- formatting the next list item (item 4)
```

### chat-dailydialog-0047 · layer 52 · pos 137 · token ' ('

Judged: `n/a`

```
- Examples of emotional or physical impacts of social anxiety (e.g., sweating, racing heart, panic, avoidance, mind going blank)
- Completing the parenthetical prompt asking how meeting people affects the user
```

### chat-lmsys-0035 · layer 36 · pos 152 · token '\n'

Judged: `n/a`

```
- Clarifying geographic scope (global snowfall vs. Northern Hemisphere vs. Earth's land area)
- Setting the timeframe (e.g., three months, December through February, or year-round snow accumulation)
- Total mass or volume of snow that falls globally in a winter season
```

### chat-dailydialog-0030 · layer 52 · pos 53 · token '.'

Judged: `n/a`

```
- closing syntax brackets (e.g. }}]])
- Douglas Adams' Hitchhiker's Guide to the Galaxy universe
- Vogon poetry or destruction/construction
- returning to Jim's conversational response or character voice
```

### chat-dailydialog-0061 · layer 44 · pos 247 · token '?'

Judged: `['consistent']`

```
- Dehydration or electrolyte imbalance causing muscle cramps/leg pain
- Explaining how dehydration leads to muscle soreness/cramping
- Offering or suggesting a glass of water/fluids
```

### chat-dailydialog-0030 · layer 52 · pos 137 · token ','

Judged: `n/a`

```
- continuation describing the sound/tune played by the ukulele
- the living house / sentient mattress / Golgafrinchan context in Hitchhiker's Guide
- Jim / Ford Prefect / Arthur Dent reference correction
```

### chat-dailydialog-0020 · layer 44 · pos 60 · token '\n\n'

Judged: `['consistent']`

```
- Offering assistance or pivoting to how the AI can help the user (e.g., 'How can I help you today?')
- The user's intended recipient 'Bill' and mistaken identity
- Concluding the friendly correction politely
```

### chat-lmsys-0061 · layer 36 · pos 149 · token ' *'

Judged: `n/a`

```
- The next Eisenhower Matrix quadrant: Urgent but Not Important (Delegate)
- The final Eisenhower Matrix quadrant: Not Urgent & Not Important (Delete / Eliminate)
- Emphasizing Quadrant 2 (Important but Not Urgent) as key to proactive time management
```

### chat-dailydialog-0054 · layer 52 · pos 85 · token '.,'

Judged: `n/a`

```
- Common film genre examples (e.g., action, comedy, sci-fi, horror, drama)
- Listing sample movie categories to help narrow down choices
- Structuring a list of suggestions for planning a cinema trip
```

### chat-lmsys-0042 · layer 20 · pos 154 · token ' .'

Judged: `n/a`

```
- Concatenating the closing wildcard '%' to the LIKE search string in PHP ('%' . $request->search . '%')
- Completing the bad practice / SQL query example before demonstrating the correct Eloquent / parameter binding approach
- The correct way to filter database queries in Laravel using Eloquent or `when()`
```

## multihop (1100 rows)

### b3-mh-operahouse-country · layer 32 · pos 11 · token ' is'

Judged: `True`

```
- Australia
- Sydney Opera House
- Sydney
```

### bf-sphinx-river · layer 52 · pos 13 · token ' the'

Judged: `True`

```
- Nile River
- Egypt
- Great Sphinx of Giza
```

### b3-mh-kangaroo-ocean · layer 36 · pos 14 · token ' the'

Judged: `True`

```
- Indian Ocean
- Australia
- Indian
```

### b3-mh-gravity-fruit · layer 52 · pos 15 · token ' the'

Judged: `True`

```
- apple
- Isaac Newton's discovery of gravity
- apple falling from a tree
```

### bf-whitebear-ocean · layer 40 · pos 14 · token ' the'

Judged: `True`

```
- Arctic Ocean
- "Arctic"
- polar bear (Ursus maritimus)
- marine habitat of polar bears
```

### inv-sunrise-opposite · layer 56 · pos -2 · token ' is'

Judged: `True`

```
- West
- East (the direction where the sun rises)
- cardinal directions
```

### b3-mh-kangaroo-ocean · layer 52 · pos 14 · token ' the'

Judged: `True`

```
- Indian Ocean
- Australia
- Indian
```

### chem-organic-Z · layer 56 · pos -2 · token ' is'

Judged: `True`

```
- 6
- carbon
- atomic number of carbon
```

### half-clock-hours · layer 56 · pos -2 · token ' is'

Judged: `True`

```
- the number six (6)
- 12 divided by 2
- 12 hours on a clock face
```

### bf-stopsign-sides · layer 56 · pos 14 · token ' is'

Judged: `True`

```
- the number eight (8)
- an octagon
- geometric properties of standard traffic stop signs
```
