import sys
def varint(b,i):
    r=0;s=0
    while True:
        x=b[i];i+=1;r|=(x&0x7f)<<s;s+=7
        if x<0x80: return r,i
def parse(b,depth=0,path=''):
    i=0;out=[]
    try:
        while i<len(b):
            k,i=varint(b,i);f=k>>3;w=k&7
            if f==0: return None
            if w==0: v,i=varint(b,i);out.append((path+'/'+str(f),'v',v))
            elif w==1: out.append((path+'/'+str(f),'f64',b[i:i+8].hex()));i+=8
            elif w==5: out.append((path+'/'+str(f),'f32',b[i:i+4].hex()));i+=4
            elif w==2:
                l,i=varint(b,i);sub=b[i:i+l];i+=l
                if i>len(b): return None
                p=parse(sub,depth+1,path+'/'+str(f)) if depth<8 and l>0 else None
                try: t=sub.decode('utf8'); pr=all(32<=ord(c)<127 or c in '\n\t' for c in t)
                except: pr=False
                if pr and l>0: out.append((path+'/'+str(f),'s',t[:120]))
                elif p is not None: out+=p
                else: out.append((path+'/'+str(f),'b',l))
            else: return None
    except IndexError: return None
    return out
