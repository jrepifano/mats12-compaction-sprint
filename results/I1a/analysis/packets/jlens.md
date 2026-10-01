# Random raw readouts: jlens

`random.Random(0)`, 10 cells per family, drawn from rows sorted by (id, layer, pos). Not cherry-picked. Pass-rate verdicts are per item (any layer).

## basic_readout_mt (1100 rows)

### brh-he-ben-zvi · layer 48 · pos 32 · token ' היה'

Judged: `False`

```
 Bernard |  Abraham |  Harold |  George |  chairman |  Benjamin |  Ernest |  Samuel |  Albert |  president
```

### brh-hu-kolozsvar · layer 32 · pos 35 · token ' neve'

Judged: `False`

```
_name |  NAME |  name |  Name | (name | çļĦåĲįç§° | .name | è¿Ļä¸ªåĲįåŃĹ | /name |  names
```

### brh-ent-antarctic-buried-chain · layer 40 · pos 18 · token ' is'

Judged: `False`

```
åĲįåŃĹ | åĲįç§° | _name | çļĦåĲįåŃĹ | è¿Ļä¸ªåĲįåŃĹ | çļĦåĲįç§° | åĳ½åĲį | åı«ä»Ģä¹Ī | åĲįä¸º | åĳ½åĲįä¸º
```

### brh-ent-lemierre-syndrome · layer 28 · pos 34 · token ' as'

Judged: `False`

```
 ________ |  ?** |  __________________ |  __________________________________ |  ==== |  _____ |  ____ |  ????? | èĳĹåĲįçļĦ |  ______
```

### brh-pl-konstantynopol-sobor-381 · layer 28 · pos 27 · token ' w'

Judged: `False`

```
ÎĴ |  cathedral | SESSION | éķ¿æ²Ļ |  Vatican |  bishop | antine | çļĦåŁİå¸Ĥ | Õµ | å¤§åİħ
```

### brh-id-terusan-suez · layer 40 · pos 29 · token 'akan'

Judged: `False`

```
 Canal |  canal | è¿Ĳæ²³ | å¤§è¿Ĳæ²³ | æ°´éģĵ |  Egypt |  Passage |  railway |  Nile |  river
```

### brh-hu-aranybulla · layer 36 · pos 46 · token ' az'

Judged: `True`

```
 Hungarian |  Hungary | åĮĪçīĻåĪ© |  sz | å®ªæ³ķ |  constitutional | constitutional |  Å¾ |  feudal |  Czech
```

### brh-ent-mughal-military-paymaster · layer 40 · pos 25 · token ' of'

Judged: `False`

```
 ______ | ____ |  ____ | ____________ | ________ |  ________ | __. | _________ |  Raj | _____
```

### brh-id-sungai-kapuas · layer 52 · pos 19 · token ' bernama'

Judged: `False`

```
ï¼¿ï¼¿ | ____ | _____ | ____________ | _________ |  ____ |  __________________ | ________ | __. | ________________
```

### brh-fi-ahvenanmaa · layer 48 · pos 50 · token ' on'

Judged: `False`

```
 Finland |  Finnish |  finland |  Aujourd |  Nordic |  Estonia |  SÃ¡ |  Suomen |  heute |  Norwegian
```

## hallucination (5615 rows)

### chat-lmsys-0054 · layer 20 · pos 298 · token '.'

Judged: `['off_topic']`

```
å¸¸è¢« | å¸¸å¸¸ | åªĴä½ĵ |  spesso | è¢«èªīä¸º |  ÑĩÐ°ÑģÑĤÐ¾ | å¤įæĿĤçļĦ | ç½ĳåıĭ | Often | æĽ´å®¹æĺĵ
```

### chat-dailydialog-0047 · layer 52 · pos 137 · token ' ('

Judged: `['consistent']`

```
 feelings |  anxiety |  emotions | Ð½Ð°Ð¿ÑĢÐ¸Ð¼ÐµÑĢ |  emotional |  Anxiety |  ÐĿÐ°Ð¿ÑĢÐ¸Ð¼ÐµÑĢ |  feeling |  adrenaline |  emotionally
```

### chat-lmsys-0035 · layer 36 · pos 152 · token '\n'

Judged: `['consistent']`

```
çļĦå¹³åĿĩ | avg | istor | æĺ¯ä¸Ģçīĩ | æĪĳä»¬åı¯ä»¥éĢļè¿ĩ | average | aremos |  hypothetical |  estimating |  calculating
```

### chat-dailydialog-0030 · layer 52 · pos 53 · token '.'

Judged: `['consistent']`

```
]( | ] | <|im_end|> | ]] | ]) | )) | ))) | } | ĊĊ | }}
```

### chat-dailydialog-0061 · layer 44 · pos 247 · token '?'

Judged: `['consistent']`

```
 dehydration |  hydration | hydration |  thirsty |  kanske | ç¼ºæ°´ |  thirst | maybe |  maybe | dehy
```

### chat-dailydialog-0030 · layer 52 · pos 137 · token ','

Judged: `['consistent']`

```
 echoing |  singing |  sounding |  audible |  lament |  haunting |  uttered |  lonely |  melanch |  voiced
```

### chat-dailydialog-0020 · layer 44 · pos 60 · token '\n\n'

Judged: `['hallucinated']`

```
 Ð²Ð°Ð¼ |  báº¡n | interesting | ä½łåı¯èĥ½ | å¦ĤæŀľæĤ¨ | èģĬèģĬ | /chat | ðŁĺĬ | å¦Ĥæŀľä½ł |  Ð¼Ð¾Ð¶ÐµÑĤÐµ
```

### chat-lmsys-0061 · layer 36 · pos 149 · token ' *'

Judged: `['generic']`

```
!* | æľĢåħ· |  ÑĩÐ°ÑīÐµ |  Ð§Ð°ÑģÑĤÐ¾ |  ÐĴÐ°Ð¶Ð½Ð¾ | æľĢèĥ½ | æĺ¯åħ³éĶ® | _key |  ×Ĺ×©×ķ×ĳ | tip
```

### chat-dailydialog-0054 · layer 52 · pos 85 · token '.,'

Judged: `['consistent']`

```
 thriller |  comedy |  blockbuster |  cinematic |  movies |  cinema | movies |  Netflix |  superhero |  Bollywood
```

### chat-lmsys-0042 · layer 20 · pos 154 · token ' .'

Judged: `['off_topic']`

```
æĿ¥çļĦ | åĺ¿ | æĸ¯åį¡ | è¿ľå¤Ħ | isma | href | à¤° | ×Ļ×¦ | ç½ĳä¸Ĭ | mnt
```

## multihop (1100 rows)

### b3-mh-operahouse-country · layer 32 · pos 11 · token ' is'

Judged: `True`

```
åĵªä¸ª |  ______ | æľīåĵªäºĽ | åĽ¾æłĩ |  ðŁ | æŁĲä¸ª | æĺ¯ä»Ģä¹ĪæĦıæĢĿ |  ___ | åħ¨çĲĥ | ____
```

### bf-sphinx-river · layer 52 · pos 13 · token ' the'

Judged: `True`

```
 Egypt |  Nile |  Egyptian |  River |  Cairo | æ²³æµģ |  river |  Egyptians |  rivers | ____________
```

### b3-mh-kangaroo-ocean · layer 36 · pos 14 · token ' the'

Judged: `False`

```
 ocean |  Ocean | æµ·æ´ĭ | å¤ªå¹³æ´ĭ | Ocean |  oceans | ä»Ģä¹Ī | ä»Ģä¹ĪæĦıæĢĿ |  ______ |  Pacific
```

### b3-mh-gravity-fruit · layer 52 · pos 15 · token ' the'

Judged: `True`

```
 apple |  apples |  Apple |  fruit |  ______ | ____ |  __________________ | ____________ |  ____ |  ________
```

### bf-whitebear-ocean · layer 40 · pos 14 · token ' the'

Judged: `False`

```
 Arctic | åĮĹæŀģ |  Antarctic |  Greenland |  Ice |  ice | åĨ°éĽª |  Antarctica |  icy |  ocean
```

### inv-sunrise-opposite · layer 56 · pos -2 · token ' is'

Judged: `True`

```
 west |  east |  called |  opposite |  where |  West |  western |  southwest | west |  sunset
```

### b3-mh-kangaroo-ocean · layer 52 · pos 14 · token ' the'

Judged: `False`

```
 Atlantic |  Africa |  Ocean |  ocean |  African |  oceans |  Antarctica |  Caribbean |  Mediterranean |  Sea
```

### chem-organic-Z · layer 56 · pos -2 · token ' is'

Judged: `False`

```
____ |  ______ |  ____ | ___ | _____ | ________ | ____________ | _________ |  ___ | ĊĊ
```

### half-clock-hours · layer 56 · pos -2 · token ' is'

Judged: `True`

```
 twelve |  equal |  eleven |  nine |  seven |  thirteen |  six |  fifteen |  thirty |  eight
```

### bf-stopsign-sides · layer 56 · pos 14 · token ' is'

Judged: `False`

```
 eight |  equal |  greater |  seven |  divisible | å¤§äºİ |  six | equal |  twelve | -eight
```
