import pandas as pd
import numpy as np
from matplotlib import pyplot as plt
from matplotlib.ticker import(AutoMinorLocator, FormatStrFormatter)
import os

current_loc = os.getcwd()
subfolder = ("/postProcessing/forceCoeffs1/0/coefficient.dat")
subfolder_mod = ("/postProcessing/forceCoeffs1/0/coefficient_mod.dat")
loc=current_loc+subfolder
loc_mod=current_loc+subfolder_mod

iter_to_avg = 50

with open(loc, 'r') as file :
  filedata = file.read()
# Replace the target string
filedata = filedata.replace('# Time', 'Time')
# Write the file out again
with open(loc_mod, 'w') as file:
  file.write(filedata)

columns_to_keep = ['Time','Cd', 'Cs', 'Cl','CmRoll','CmPitch','CmYaw','Cd(f)','Cd(r)','Cs(f)','Cs(r)','Cl(f)','Cl(r)']
df = pd.read_table(loc_mod, usecols=columns_to_keep, sep="\s+",skiprows=12)
M=df.to_numpy()
total_iter = df.shape[0]
#print(total_iter)

## CL CONVERGENCE

CL=M[:,3]
CL=CL[-iter_to_avg:]

CL_avg=np.average(CL)
CL_std=np.std(CL)
CL_ratio=abs(CL_std/CL_avg*100);

ax1=df.plot(x='Time', y='Cl')
plt.axhline(y=CL_avg, color='r', linestyle='--',linewidth=1)
ax1.set_ylabel('$C_{L}$')
ax1.set_xlabel('Iteration')
ax1.set_title('Lift Convergence - All')
ax1.get_legend().remove()

plt.savefig('01_Convergence_CL_All.png')

ax1=df.iloc[(total_iter-iter_to_avg):total_iter].plot(x='Time', y='Cl')
plt.axhline(y=CL_avg, color='r', linestyle='--',linewidth=1)
ax1.set_ylabel('$C_{L}$')
ax1.set_xlabel('Iteration')
ax1.set_title('Lift Convergence - Last 100 Iterations')
ax1.get_legend().remove()

majorFormatter=FormatStrFormatter('%.4f')
textstr = '\n'.join((
    r'$\mu=%.4f$' % (CL_avg, ),
    r'$\sigma=%.4f$' % (CL_std, ),
    r'$\sigma/\mu=%.2f$' % (CL_ratio, )))+'%'
if CL_ratio<=0.1:
    props = dict(boxstyle='round',facecolor='lightgreen',alpha=1)
elif CL_ratio>0.1 and CL_ratio<=1.0:
    props = dict(boxstyle='round',facecolor='gold',alpha=1)
else:
    props = dict(boxstyle='round',facecolor='red',alpha=1)

ax1.text(0.8,0.85,textstr,transform=ax1.transAxes,fontsize=9,bbox=props)
plt.grid()

plt.savefig('02_Convergence_CL_Last.png')

## CD CONVERGENCE

CD=M[:,1]
CD=CD[-iter_to_avg:]

CD_avg=np.average(CD)
CD_std=np.std(CD)
CD_ratio=abs(CD_std/CD_avg*100);

ax1=df.plot(x='Time', y='Cd')
plt.axhline(y=CD_avg, color='r', linestyle='--',linewidth=1)
ax1.set_ylabel('$C_{D}$')
ax1.set_xlabel('Iteration')
ax1.set_title('Drag Convergence - All')
ax1.get_legend().remove()

plt.savefig('03_Convergence_CD_All.png')

ax1=df.iloc[(total_iter-iter_to_avg):total_iter].plot(x='Time', y='Cd')
plt.axhline(y=CD_avg, color='r', linestyle='--',linewidth=1)
ax1.set_ylabel('$C_{D}$')
ax1.set_xlabel('Iteration')
ax1.set_title('Drag Convergence - Last 100 Iterations')
ax1.get_legend().remove()

majorFormatter=FormatStrFormatter('%.4f')
textstr = '\n'.join((
    r'$\mu=%.4f$' % (CD_avg, ),
    r'$\sigma=%.4f$' % (CD_std, ),
    r'$\sigma/\mu=%.2f$' % (CD_ratio, )))+'%'
if CD_ratio<=0.1:
    props = dict(boxstyle='round',facecolor='lightgreen',alpha=1)
elif CD_ratio>0.1 and CD_ratio<=1.0:
    props = dict(boxstyle='round',facecolor='gold',alpha=1)
else:
    props = dict(boxstyle='round',facecolor='red',alpha=1)

ax1.text(0.8,0.85,textstr,transform=ax1.transAxes,fontsize=9,bbox=props)
plt.grid()

plt.savefig('04_Convergence_CD_Last.png')