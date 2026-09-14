#include "joint_motion.hpp"
#include <cassert>
int main(){
 using namespace joint_motion;Pose q{};q[18]=q[25]=std::acos(-1.)/2;
 int elbows[]{18,25};double forward[]{0,0};Plan p;p.start(q,elbows,forward,2,3);
 assert(p.duration==3);
 for(int i=0;i<601;++i){auto before=p.q;p.step(.005);assert(std::abs(p.q[18]-before[18])/.005<1.101);assert(p.q[31]==0);}
 assert(p.q[18]==0 && p.q[25]==0);
 Pose feedback=p.q;feedback[18]=.110626;feedback[25]=.0978632;assert(p.reached(feedback));
 feedback[18]=.150001;assert(!p.reached(feedback));
 int wrist[]{19};double angle[]{50*std::acos(-1.)/180};p.start(p.q,wrist,angle,1,1.5);assert(p.duration==1.5);
 for(int i=0;i<301;++i)p.step(.005);assert(p.q[19]==angle[0]);
 int shoulder[]{15};double raised[]{-.5235987755982988};p.start(p.q,shoulder,raised,1,3);
 feedback=p.target;feedback[15]=-.4186449348926544;assert(p.reached(feedback));
 feedback[15]=raised[0]+.150001;assert(!p.reached(feedback));
 int bad[]{12};bool rejected=false;try{p.start(p.q,bad,angle,1,1.5);}catch(...){rejected=true;}assert(rejected);
}
