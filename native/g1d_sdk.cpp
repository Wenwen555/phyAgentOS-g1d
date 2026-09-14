// Device-only ABI. ChannelFactory is process-global: use one instance per Dora node.
// Loading this library does not initialize DDS or send commands.
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <thread>

#include <unitree/idl/hg/LowState_.hpp>
#include <unitree/idl/hg/IMUState_.hpp>
#include <unitree/idl/ros2/Point32_.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/robot/g1/agv/g1_agv_client.hpp>
#include "joint_motion.hpp"
#include <unitree/idl/hg/LowCmd_.hpp>
#include <unitree/robot/channel/channel_publisher.hpp>
#include <sys/file.h>
#include <fcntl.h>
#include <unistd.h>
#include <iostream>

using Clock = std::chrono::steady_clock;
using LowState = unitree_hg::msg::dds_::LowState_;
using Point = geometry_msgs::msg::dds_::Point32_;
using namespace unitree::robot;

extern "C" {
struct G1dState {
  double height, height_age_s, joint_age_s;
  uint64_t height_seq, joint_seq;
  double q[16], dq[16];
  int32_t mode_machine, watchdog_tripped;
};
}

namespace {
constexpr std::array<int, 16> joints{12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28};
template<class T> uint32_t crc32(const T &state) {
  uint32_t crc = 0xffffffff;
  const auto *bytes = reinterpret_cast<const unsigned char *>(&state);
  for (size_t i = 0; i < sizeof(state) / 4 - 1; ++i) {
    uint32_t word;
    std::memcpy(&word, bytes + i * 4, 4);
    for (uint32_t bit = 0x80000000; bit; bit >>= 1) {
      const bool high = (crc & 0x80000000) != 0;
      crc <<= 1;
      if (high) crc ^= 0x04c11db7;
      if (word & bit) crc ^= 0x04c11db7;
    }
  }
  return crc;
}

#include "sdk_joint_driver.hpp"

struct Device {
  SdkJointDriver arm;
  std::mutex state_mutex, command_mutex;
  G1dState state{};
  Clock::time_point height_time{}, joint_time{}, command_time{};
  ChannelSubscriberPtr<Point> height_sub;
  ChannelSubscriberPtr<LowState> joints_sub;
  ChannelSubscriberPtr<unitree_hg::msg::dds_::IMUState_> torso_sub;
  std::array<double,3> torso_rpy{};
  Clock::time_point torso_time{};
  std::unique_ptr<g1::AgvClient> client;
  std::atomic<bool> closing{false};
  std::thread watchdog;
  bool moving = false, tripped = false;
  double minimum = 0, maximum = 0, command_limit = 0;

  Device(const char *nic, int domain, bool enabled, double low, double high, double limit)
      : minimum(low), maximum(high), command_limit(limit) {
    ChannelFactory::Instance()->Init(domain, nic);
    torso_sub = std::make_shared<ChannelSubscriber<unitree_hg::msg::dds_::IMUState_>>("rt/secondary_imu");
    torso_sub->InitChannel([this](const void* raw) {
      const auto& imu = *static_cast<const unitree_hg::msg::dds_::IMUState_*>(raw);
      for (float value : imu.rpy()) if (!std::isfinite(value)) return;
      std::lock_guard<std::mutex> lock(state_mutex);
      for (int i=0;i<3;++i) torso_rpy[i]=imu.rpy()[i];
      torso_time=Clock::now();
    }, 1);
    height_sub = std::make_shared<ChannelSubscriber<Point>>("rt/hispeed_state");
    height_sub->InitChannel([this](const void *raw) {
      const double height = static_cast<const Point *>(raw)->y();
      if (!std::isfinite(height)) return;
      std::lock_guard<std::mutex> lock(state_mutex);
      state.height = height;
      ++state.height_seq;
      height_time = Clock::now();
    }, 1);
    joints_sub = std::make_shared<ChannelSubscriber<LowState>>("rt/lowstate");
    joints_sub->InitChannel([this](const void *raw) {
      const auto &msg = *static_cast<const LowState *>(raw);
      if (msg.crc() != crc32(msg)) return;
      arm.observe(msg);
      for (int index : joints) {
        if (!std::isfinite(msg.motor_state()[index].q()) ||
            !std::isfinite(msg.motor_state()[index].dq())) return;
      }
      std::lock_guard<std::mutex> lock(state_mutex);
      for (size_t i = 0; i < joints.size(); ++i) {
        state.q[i] = msg.motor_state()[joints[i]].q();
        state.dq[i] = msg.motor_state()[joints[i]].dq();
      }
      state.mode_machine = msg.mode_machine();
      ++state.joint_seq;
      joint_time = Clock::now();
    }, 1);
    if (enabled) {
      client = std::make_unique<g1::AgvClient>();
      client->SetTimeout(0.1f);
      client->Init();
      watchdog = std::thread([this] {
        while (!closing.load()) {
          std::this_thread::sleep_for(std::chrono::milliseconds(20));
          std::lock_guard<std::mutex> command_lock(command_mutex);
          bool stale;
          {
            std::lock_guard<std::mutex> state_lock(state_mutex);
            stale = !state.height_seq || Clock::now() - height_time > std::chrono::milliseconds(300);
          }
          if (moving && (stale || Clock::now() - command_time > std::chrono::milliseconds(250))) {
            tripped = true;
            try { if (client->HeightAdjust(0.0f) == 0) moving = false; }
            catch (...) { /* Remain uncertain and try the zero command on the next tick. */ }
          }
        }
      });
    }
  }

  ~Device() {
    closing.store(true);
    if (watchdog.joinable()) watchdog.join();
    if (client && moving) {
      try { client->HeightAdjust(0.0f); } catch (...) {}
    }
    joints_sub->CloseChannel();
    torso_sub->CloseChannel();
    height_sub->CloseChannel();
  }
};
std::atomic<bool> allocated{false};
}  // namespace

extern "C" {
void *g1d_open(const char *nic, int domain, int enabled, double minimum, double maximum, double limit) {
  if (!nic || !*nic || domain < 0 || (enabled != 0 && enabled != 1)) return nullptr;
  if (enabled && (!std::isfinite(minimum) || !std::isfinite(maximum) || minimum >= maximum ||
                  !std::isfinite(limit) || limit <= 0 || limit > 1)) return nullptr;
  if (allocated.exchange(true)) return nullptr;
  try { return new Device(nic, domain, enabled, minimum, maximum, limit); }
  catch (...) { allocated.store(false); return nullptr; }
}

int g1d_read(void *handle, G1dState *output) {
  if (!handle || !output) return -1;
  auto &device = *static_cast<Device *>(handle);
  std::lock_guard<std::mutex> command_lock(device.command_mutex);
  std::lock_guard<std::mutex> state_lock(device.state_mutex);
  *output = device.state;
  output->height_age_s = output->height_seq ? std::chrono::duration<double>(Clock::now() - device.height_time).count() : -1;
  output->joint_age_s = output->joint_seq ? std::chrono::duration<double>(Clock::now() - device.joint_time).count() : -1;
  output->watchdog_tripped = device.tripped;
  return 0;
}

int g1d_height_velocity(void *handle, double velocity) {
  if (!handle || !std::isfinite(velocity)) return -1;
  auto &device = *static_cast<Device *>(handle);
  std::lock_guard<std::mutex> command_lock(device.command_mutex);
  if (!device.client || std::abs(velocity) > device.command_limit) return -2;
  if (velocity != 0) {
    std::lock_guard<std::mutex> state_lock(device.state_mutex);
    if (device.tripped || !device.state.height_seq ||
        Clock::now() - device.height_time > std::chrono::milliseconds(300)) return -3;
    if (device.state.height < device.minimum || device.state.height > device.maximum ||
        (velocity < 0 && device.state.height <= device.minimum) ||
        (velocity > 0 && device.state.height >= device.maximum)) return -4;
  }
  try {
    // Mark motion uncertain before RPC: a timeout can occur after the effect was applied.
    if (velocity != 0) device.moving = true;
    device.command_time = Clock::now();
    const int result = device.client->HeightAdjust(static_cast<float>(velocity));
    if (result == 0 && velocity == 0) device.moving = false;
    return result;
  } catch (...) { return -5; }
}

int g1d_arm_move(void* handle,const int* ids,const double* values,int count,double seconds){
 if(!handle || !ids || !values)return -1;
 return static_cast<Device*>(handle)->arm.start(ids,values,count,seconds);
}
int g1d_arm_read(void* handle,double* q,double* target,double* age,double* duration,int* phase){
 if(!handle || !q || !target || !age || !duration || !phase)return -1;
 auto& a=static_cast<Device*>(handle)->arm;std::lock_guard<std::mutex> l(a.mutex);
 for(int j=0;j<35;++j){q[j]=a.measured[j];target[j]=a.owned?a.reference[j]:a.measured[j];}
 *age=a.age();*duration=a.plan.duration;*phase=a.phase;return 0;
}
int g1d_arm_cancel(void* handle){if(!handle)return -1;static_cast<Device*>(handle)->arm.cancel();return 0;}

int g1d_torso_read(void* handle,double* rpy,double* age){
 if(!handle || !rpy || !age)return -1;
 auto& d=*static_cast<Device*>(handle);std::lock_guard<std::mutex> lock(d.state_mutex);
 for(int i=0;i<3;++i)rpy[i]=d.torso_rpy[i];
 *age=d.torso_time==Clock::time_point{}?-1:std::chrono::duration<double>(Clock::now()-d.torso_time).count();
 return 0;
}

void g1d_close(void *handle) {
  if (handle) delete static_cast<Device *>(handle);
  // ChannelFactory cannot reliably be reconfigured in the same process.
}
}
