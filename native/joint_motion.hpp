#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <stdexcept>
namespace joint_motion {
using Pose=std::array<double,35>;
constexpr std::array<int,18> active{12,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,31,33};
// Commissioned application envelope, not manufacturer mechanical limits.
inline bool allowed(int j,double q){
 if(!std::isfinite(q))return false;
 if(j==31 || j==33)return q>=0 && q<=5.4;
 if(j==18 || j==25)return q>=-.2 && q<=2.;
 if(j==15 || j==22 || j==19 || j==26)return std::abs(q)<=1.5707963267948966;
 if(j==16)return q>=-.2 && q<=1.2;
 if(j==23)return q>=-1.2 && q<=.2;
 if(j==17 || j==24 || j==20 || j==21 || j==27 || j==28)return std::abs(q)<=.5;
 return false;
}
inline double tolerance(int j){return (j>=15 && j<=18) || (j>=22 && j<=25) || j==31 || j==33?.15:.08;}
inline double speed(int j){return j==31 || j==33?7.:(j==18 || j==25?1.1:(j==19 || j==26?1.1:.65));}
struct Plan {
 Pose from{},target{},q{},dq{};
 std::array<bool,35> selected{};
 double duration=0,elapsed=0;
 void start(const Pose& reference,const int* ids,const double* values,int count,double seconds){
  if(count<1 || count>16 || !std::isfinite(seconds) || seconds<.5 || seconds>30)throw std::invalid_argument("invalid motion request");
  Plan next;next.from=next.q=next.target=reference;next.duration=seconds;
  for(int k=0;k<count;++k){int j=ids[k];if(j<0 || j>=35 || next.selected[j] || !allowed(j,values[k]))throw std::invalid_argument("joint outside application envelope or repeated");
   next.selected[j]=true;next.target[j]=values[k];
   next.duration=std::max(next.duration,1.875*std::abs(values[k]-reference[j])/speed(j));
  }
  *this=next;
 }
 void step(double dt){
  elapsed=std::min(duration,elapsed+std::clamp(dt,0.,.02));double u=elapsed/duration;
  double b=u*u*u*(10+u*(-15+6*u));
  for(int j:active){q[j]=from[j]+(target[j]-from[j])*b;dq[j]=(target[j]-from[j])*30*u*u*(1-u)*(1-u)/duration;}
  if(elapsed>=duration){q=target;dq.fill(0);}
 }
 bool reached(const Pose& measured)const{
  for(int j:active)if(selected[j] && (!std::isfinite(measured[j]) || std::abs(measured[j]-target[j])>tolerance(j)))return false;
  return true;
 }
};
}
