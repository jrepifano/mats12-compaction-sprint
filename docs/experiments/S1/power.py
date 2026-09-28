from math import sqrt
from statistics import NormalDist
z=NormalDist().inv_cdf
def mde_vs_p0(n,p0,alpha,power=0.8,deff=1.0,one_sided=True):
    za = z(1-alpha) if one_sided else z(1-alpha/2)
    return (za+z(power))*sqrt(p0*(1-p0)/n*deff)
def mde_paired(n,disc,alpha=0.05,power=0.8,deff=1.0):
    return (z(1-alpha/2)+z(power))*sqrt(disc/n*deff)
def ci_half(n,p,deff=1.0): return 1.96*sqrt(p*(1-p)/n*deff)
print('Classifier test n=600: CI half-width at p=.6, deff1.5: %.3f'%ci_half(600,.6,1.5))
print('Classifier test n=600: CI half-width at p=.3, deff1.5: %.3f'%ci_half(600,.3,1.5))
print('Classifier paired clean-vs-rewrite n=600 disc=.30 deff1.5 MDE: %.3f'%mde_paired(600,.30,deff=1.5))
print('Classifier OpenAI-vs-rest AUROC: n_pos=100,n_neg=500 -> SE(AUC=.65) approx (Hanley-McNeil)')
def hanley(A,n1,n2):
    Q1=A/(2-A); Q2=2*A*A/(1+A)
    return sqrt((A*(1-A)+(n1-1)*(Q1-A*A)+(n2-1)*(Q2-A*A))/(n1*n2))
for A in (.55,.65,.8): print('  AUC',A,'SE %.3f'%hanley(A,100,500),'CI half %.3f'%(1.96*hanley(A,100,500)))
print('Investigator per-post n=300 vs chance 1/6, one-sided alpha=.05/6 (Holm worst), deff1.3: MDE %.3f -> detect acc >= %.3f'%(mde_vs_p0(300,1/6,.05/6,deff=1.3),1/6+mde_vs_p0(300,1/6,.05/6,deff=1.3)))
print('Investigator per-post paired clean-vs-rewrite n=300 disc=.35 deff1.3 MDE %.3f'%mde_paired(300,.35,deff=1.3))
print('Pooled 6 investigators paired n=1800 disc=.35 deff1.3 MDE %.3f'%mde_paired(1800,.35,deff=1.3))
print('Batch n=60 per inv-cond vs 1/6 one-sided .05: MDE %.3f -> acc >= %.3f'%(mde_vs_p0(60,1/6,.05),1/6+mde_vs_p0(60,1/6,.05)))
print('Pairwise n=80 pairs per inv-cond vs .5, one-sided .05/6: MDE %.3f -> acc >= %.3f'%(mde_vs_p0(80,.5,.05/6),.5+mde_vs_p0(80,.5,.05/6)))
print('Pairwise pooled 6 inv n=480 vs .5 one-sided .05: MDE %.3f'%mde_vs_p0(480,.5,.05,deff=1.2))
# own-family advantage (column-wise): per family cell n=50 posts; diff acc(f,f)-mean_{j!=f}acc(j,f)
# var approx p(1-p)/50*(1+1/5) ignoring positive within-post correlation (conservative); global = mean of 6
import math
p=.3
se_cell=math.sqrt(p*(1-p)/50*(1+1/5)); se_glob=se_cell/math.sqrt(6)
print('Own-family advantage global SE %.3f, MDE two-sided .05 %.3f'%(se_glob,(1.96+0.84)*se_glob))
print('Real-post transfer n=120 vs 1/6 one-sided .05 deff1.5: MDE %.3f'%mde_vs_p0(120,1/6,.05,deff=1.5))
print('Fact retention: 600 posts x ~6 facts=3600 facts; CI half-width at .9 with deff 2: %.3f'%ci_half(3600,.9,2))
