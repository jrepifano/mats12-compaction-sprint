# S1 budget v2. Prices $/Mtok (in,out): OpenRouter /api/v1/models, 2026-09-28.
P = {'openai/gpt-5.6-sol':(2.0,10.0),'anthropic/claude-sonnet-5':(2.0,10.0),'google/gemini-3.8-flash':(0.75,3.75),
     'moonshotai/kimi-k3':(3.0,15.0),'z-ai/glm-5.3':(1.4,4.4),'meta/muse-spark-1.3':(1.25,4.25),
     'deepseek/deepseek-v4-flash':(0.14,0.28),'qwen/qwen3.7-flash':(0.03,0.13),'openai/gpt-6-luna':(0.10,0.50),'z-ai/glm-5.3-flash':(0.15,0.50)}
FAM=list(P)[:6]  # writers = investigators
def c(m,i,o): pi,po=P[m]; return (i*pi+o*po)/1e6
def S(i,o): return sum(c(m,i,o) for m in FAM)
def run(RW=200, RI=100, show=True):
    it=[]
    it.append(('B1 Writers: 6 x 325 posts (x1.25 regen); 900 in / 180+%d out'%RW, S(900,180+RW)*325*1.25))
    it.append(('B2 Rewrites: 3 ops x 1,950 posts, DeepSeek/Qwen 50:50; 350 in / 220 out, +10% retries',
               3*1950*(0.5*c('deepseek/deepseek-v4-flash',350,220)+0.5*c('qwen/qwen3.7-flash',350,220))*1.10))
    J=lambda i,o: c('openai/gpt-6-luna',i,o)+c('z-ai/glm-5.3-flash',i,o)   # two-family judge panel
    # worst case (r1 finding 15): every required fact of every rewrite goes to both judges
    # 5,850 rewrites x ~3 facts = 17,550; screening includes the 1.25 regeneration allowance
    it.append(('B3 Luna+GLM-flash: screen 2,438 (400/40) + fact retention worst case 17,550 (600/60)',
               J(400,40)*1950*1.25 + J(600,60)*17550))
    it.append(('B4 Per-post 6-way: 300 posts x 4 cond x 6 inv; 450 in / 30+%d out'%RI, S(450,30+RI)*1200))
    it.append(('B5 Batch 6-way (k=10): 60 batches x 4 cond x 6 inv; 2,250 in / 30+%d out'%RI, S(2250,30+RI)*240))
    it.append(('B6 Pairwise self-rec: 80 pairs x 2 orders x 3 cond x 6 inv; 650 in / 30+%d out'%RI, S(650,30+RI)*480))
    it.append(('B7 Real-post transfer: 120 posts x 2 cond (clean,S) x 6 inv + 120 DeepSeek summaries', S(450,30+RI)*240 + c('deepseek/deepseek-v4-flash',350,220)*120))
    it.append(('B8 Reasoning ablation: Sol effort=medium, 150 clean posts; 450 in / 1,530 out', c('openai/gpt-5.6-sol',450,1530)*150))
    it.append(('B9 Pilot (dev seeds): 30 posts x 2 cond x 6 inv + 20 pairwise x 6 inv', S(450,30+RI)*60+S(650,30+RI)*20))
    tot=sum(v for _,v in it)
    if show:
        for k,v in it: print(f'| {k} | {v:.2f} |')
        print(f'| TOTAL | {tot:.2f} |')
    return tot
run()
print()
for RW,RI in [(0,0),(200,100),(400,200),(400,400),(800,400),(800,800)]:
    print(RW,RI,'%.2f'%run(RW,RI,False))
for m in FAM: print(m,'inv/call %.5f'%c(m,450,130),'writer/call %.5f'%c(m,900,380),'batch %.5f'%c(m,2250,130),'pair %.5f'%c(m,650,130))
