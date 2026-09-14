#pragma once
#include "joint_motion.hpp"
#include <unitree/idl/hg/LowCmd_.hpp>
#include <unitree/robot/channel/channel_publisher.hpp>
#include <sys/file.h>
#include <fcntl.h>
#include <unistd.h>
#include <iostream>

// Parameterized SDK loop: LowCmd, CRC and 5-ms publication as arm_sequence.
// Arm gains follow Unitree xr_teleoperate G1_29_ArmController.
// SDK initialization and writes NEVER hold the feedback mutex.
struct SdkJointDriver {
 std::mutex mutex,admission_mutex;
 joint_motion::Pose measured{},reference{};
 joint_motion::Plan plan;
 std::unique_ptr<unitree::robot::ChannelPublisher<unitree_hg::msg::dds_::LowCmd_>> pub;
 std::thread worker;
 std::atomic<bool> closing{false};
 Clock::time_point received{},last{},stable{};
 uint64_t sequence=0,write_failures=0,stale_ticks=0;
 int mode=0,phase=0,lock_fd=-1;
 bool owned=false;
 std::string error;
 ~SdkJointDriver(){
  closing=true;
  if(worker.joinable())worker.join();
  if(pub)pub->CloseChannel();
  if(lock_fd>=0){flock(lock_fd,LOCK_UN);::close(lock_fd);}
 }
 void observe(const LowState& s){
  std::lock_guard<std::mutex> l(mutex);
  for(int j:joint_motion::active)if(!std::isfinite(s.motor_state()[j].q()))return;
  for(int j:joint_motion::active)measured[j]=s.motor_state()[j].q();
  mode=s.mode_machine();received=Clock::now();++sequence;
 }
 double age()const{return sequence?std::chrono::duration<double>(Clock::now()-received).count():-1;}
 int start(const int* ids,const double* values,int count,double seconds){
  std::lock_guard<std::mutex> admission(admission_mutex);
  {
   std::lock_guard<std::mutex> l(mutex);
   if(phase==1)return -2;
   if(!sequence || age()>.1)return -3;
   try{joint_motion::Plan candidate;candidate.start(owned?reference:measured,ids,values,count,seconds);}
   catch(...){return -4;}
  }
  // Creating DDS entities can take longer than 100 ms. Feedback must keep updating here.
  if(!pub){
   lock_fd=::open("/tmp/paos-g1d-arm.lock",O_CREAT|O_RDWR,0600);
   if(lock_fd<0 || flock(lock_fd,LOCK_EX|LOCK_NB)!=0){
    if(lock_fd>=0)::close(lock_fd);
    lock_fd=-1;return -5;
   }
   try{
    pub=std::make_unique<unitree::robot::ChannelPublisher<unitree_hg::msg::dds_::LowCmd_>>("rt/lowcmd");
    pub->InitChannel();
   }catch(...){pub.reset();flock(lock_fd,LOCK_UN);::close(lock_fd);lock_fd=-1;return -6;}
  }
  {
   std::lock_guard<std::mutex> l(mutex);
   if(!sequence || age()>.1)return -3;
   // Capture fresh feedback AFTER initialization, not the pre-initialization snapshot.
   if(!owned)reference=measured;
   plan.start(reference,ids,values,count,seconds);
   owned=true;phase=1;error.clear();stable={};last=Clock::now();
  }
  if(!worker.joinable())worker=std::thread([this]{run();});
  return 0;
 }
 void cancel(){std::lock_guard<std::mutex> l(mutex);if(phase==1){phase=3;plan.dq.fill(0);}}
 void run(){
  auto next=Clock::now(),reported=next;
  while(!closing){
   next+=std::chrono::milliseconds(5);
   unitree_hg::msg::dds_::LowCmd_ cmd;
   {
    std::lock_guard<std::mutex> l(mutex);
    auto now=Clock::now();double dt=std::chrono::duration<double>(now-last).count();last=now;
    bool fresh=sequence && age()<=.1;
    if(phase==1){
     if(fresh){
      plan.step(dt);reference=plan.q;
      if(plan.elapsed>=plan.duration && plan.reached(measured)){
       if(stable==Clock::time_point{})stable=now;
       if(now-stable>=std::chrono::milliseconds(300))phase=2;
      }else stable={};
     }else{
      // Same as arm_sequence: pause reference progress; don't latch an interface fault.
      ++stale_ticks;stable={};plan.dq.fill(0);
     }
    }
    cmd.mode_pr()=0;cmd.mode_machine()=mode;
    for(int j:joint_motion::active){
     auto& m=cmd.motor_cmd()[j];bool grip=j==31 || j==33;
     m.mode()=1;m.q()=grip?std::clamp(reference[j],measured[j]-.18,measured[j]+.18):reference[j];
     m.dq()=!grip && phase==1?plan.dq[j]:0;m.tau()=0;
     bool arm=j>=15 && j<=28;
     bool wrist=(j>=19 && j<=21) || (j>=26 && j<=28);
     m.kp()=grip?5:(arm?(wrist?40:80):(j==12?60:40));
     m.kd()=grip?.05:(arm?(wrist?1.5:3):1);
    }
   }
   cmd.crc()=crc32(cmd);
   bool sent=false;
   try{sent=pub->Write(cmd);}catch(...){}
   {
    std::lock_guard<std::mutex> l(mutex);
    if(!sent){++write_failures;error="DDS_WRITE_FAILED";}
    else error.clear();
    if(Clock::now()-reported>=std::chrono::seconds(1)){
     std::cout<<"SDK_JOINT phase="<<phase<<" age_s="<<age()
      <<" elbow="<<measured[18]<<","<<measured[25]<<" wrist="<<measured[19]
      <<" dex1="<<measured[31]<<" reference="<<reference[18]<<","<<reference[25]<<","<<reference[19]<<","<<reference[31]
      <<" stale_ticks="<<stale_ticks<<" write_failures="<<write_failures<<" error="<<error<<std::endl;
     reported=Clock::now();
    }
   }
   if(Clock::now()>next)next=Clock::now();
   std::this_thread::sleep_until(next);
  }
 }
};
