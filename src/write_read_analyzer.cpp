#include <algorithm>
#include <array>
#include <atomic>
#include <bit>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <functional>
#include <future>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>
#include <fcntl.h>
#include <openssl/evp.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>

namespace fs = std::filesystem;
using U64 = std::uint64_t;
using U128 = unsigned __int128;
using Clock = std::chrono::steady_clock;
constexpr U64 MAX64 = std::numeric_limits<U64>::max();
constexpr U64 ADDRESS_MASK = 0x000fffffffffffffULL;
constexpr U64 WRITE = 1ULL << 63;

void require(bool ok, const std::string& message) {
  if (!ok) throw std::runtime_error(message);
}
void require(bool ok, const char* message) {
  if (!ok) throw std::runtime_error(message);
}
U64 add(U64 a, U64 b) {
  require(b <= MAX64 - a, "64-bit counter overflow");
  return a + b;
}
std::string decimal(U128 value) {
  if (!value) return "0";
  std::string result;
  while (value) { result.push_back(static_cast<char>('0' + value % 10)); value /= 10; }
  std::reverse(result.begin(), result.end());
  return result;
}
std::string quoted(const std::string& s) {
  std::ostringstream out;
  out << '"';
  for (unsigned char c : s) {
    if (c == '"' || c == '\\') out << '\\' << c;
    else if (c < 32) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << unsigned(c);
    else out << c;
  }
  out << '"';
  return out.str();
}
U64 load64(const unsigned char* p) {
  U64 v; std::memcpy(&v, p, 8);
  if constexpr (std::endian::native == std::endian::big) v = __builtin_bswap64(v);
  return v;
}
U64 hash_line(U64 x) {
  x += 0x9e3779b97f4a7c15ULL;
  x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
  x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
  return x ^ (x >> 31);
}
struct Hash { std::size_t operator()(U64 x) const { return hash_line(x); } };
unsigned bin_for(U64 v) { return std::bit_width(v); }
std::pair<U64,U64> bounds(unsigned bin) {
  if (!bin) return {0,0};
  return {1ULL << (bin-1), bin == 64 ? MAX64 : (1ULL << bin)-1};
}

struct Metric {
  std::array<U64,65> bins{};
  U64 count = 0, minimum = MAX64, maximum = 0;
  // count <= input valid records <= (INT64_MAX-32)/16; hence sum < 2^123.
  U128 sum = 0;
  void sample(U64 v) {
    ++count; ++bins[bin_for(v)]; sum += v;
    minimum = std::min(minimum,v); maximum = std::max(maximum,v);
  }
  void merge(const Metric& b) {
    count = add(count,b.count); sum += b.sum;
    minimum = std::min(minimum,b.minimum); maximum = std::max(maximum,b.maximum);
    for (unsigned i=0; i<65; ++i) bins[i] = add(bins[i],b.bins[i]);
  }
  void verify(U64 matches) const {
    U64 n=0; for (auto c: bins) n=add(n,c);
    require(n == matches && count == matches, "histogram conservation failure");
  }
  std::string json() const {
    std::ostringstream o; o << std::setprecision(18);
    o << "{\"count\":" << count << ",\"sum\":" << quoted(decimal(sum));
    o << ",\"minimum\":" << (count ? std::to_string(minimum) : "null");
    o << ",\"maximum\":" << (count ? std::to_string(maximum) : "null");
    o << ",\"mean\":";
    if (count) o << static_cast<long double>(sum)/count; else o << "null";
    o << ",\"percentile_intervals\":{";
    bool first=true;
    for (unsigned p: {50,90,95,99}) {
      if (!first) o << ',';
      first=false; o << quoted("p"+std::to_string(p)) << ':';
      if (!count) { o << "null"; continue; }
      U64 rank=static_cast<U64>((U128(count)*p+99)/100), cumulative=0;
      for (unsigned i=0; i<65; ++i) {
        cumulative=add(cumulative,bins[i]);
        if (cumulative>=rank) {
          auto [lo,hi]=bounds(i);
          o << "{\"lower\":" << lo << ",\"upper\":" << hi << '}'; break;
        }
      }
    }
    o << "},\"histogram_counts\":[";
    for (unsigned i=0; i<65; ++i) { if (i) o << ','; o << bins[i]; }
    o << "]}"; return o.str();
  }
};
struct Stats {
  U64 reads=0,writes=0,matched=0,superseded=0,pending=0,unpaired_reads=0;
  Metric time,operations;
  void merge(const Stats& b) {
    reads=add(reads,b.reads); writes=add(writes,b.writes); matched=add(matched,b.matched);
    superseded=add(superseded,b.superseded); pending=add(pending,b.pending);
    unpaired_reads=add(unpaired_reads,b.unpaired_reads);
    time.merge(b.time); operations.merge(b.operations);
  }
  void verify() const {
    require(writes==add(add(matched,superseded),pending), "write conservation failure");
    require(reads==add(matched,unpaired_reads), "read conservation failure");
    time.verify(matched); operations.verify(matched);
  }
  std::string json() const {
    std::ostringstream o; o << std::setprecision(18);
    o << "{\"valid_accesses\":" << add(reads,writes) << ",\"reads\":" << reads
      << ",\"writes\":" << writes << ",\"matched_writes\":" << matched
      << ",\"superseded_writes\":" << superseded << ",\"pending_writes_at_end\":" << pending
      << ",\"reads_without_pending_write\":" << unpaired_reads << ",\"write_match_rate\":";
    if (writes) o << static_cast<long double>(matched)/writes; else o << "null";
    o << ",\"zero_time_matches\":" << time.bins[0] << ",\"zero_operation_matches\":" << operations.bins[0]
      << ",\"time_cycles\":" << time.json() << ",\"intervening_operations\":" << operations.json() << '}';
    return o.str();
  }
};

struct Options {
  std::string input,output;
  unsigned workers=16,decoders=4,prefetch=8,queue_batches=4;
  U64 block_records=262144;
  U64 timeline_bin_cycles=0; // Optional; 40,000,000 cycles = 0.1 s at 400 MHz.
  double progress_seconds=5;
  std::optional<U64> max_records;
  unsigned test_delay_first_block_ms=0;
};
U64 number(const std::string& s) {
  require(!s.empty() && s.find_first_not_of("0123456789")==std::string::npos,"invalid integer: "+s);
  std::size_t pos; auto n=std::stoull(s,&pos); require(pos==s.size(),"invalid integer"); return n;
}
Options options(int argc,char** argv) {
  Options o;
  for (int i=1;i<argc;++i) {
    std::string k=argv[i];
    if (k=="--help") {
      std::cout << "write_read_analyzer --input CART.bin --output NEW_DIR [--workers 16] [--decode-workers 4]\n"
                << "  [--block-records 262144] [--prefetch 8] [--queue-batches 4] [--progress-seconds 5]\n"
                << "  [--timeline-bin-cycles 40000000]  # 0 disables execution timeline\n"
                << "  [--max-records N]  # smoke only: never emits COMPLETE\n";
      std::exit(0);
    }
    require(i+1<argc,"missing value for "+k); std::string v=argv[++i];
    if (k=="--input") o.input=v;
    else if (k=="--output") o.output=v;
    else if (k=="--block-records") o.block_records=number(v);
    else if (k=="--timeline-bin-cycles") o.timeline_bin_cycles=number(v);
    else if (k=="--max-records") o.max_records=number(v);
    else if (k=="--progress-seconds") {
      std::size_t pos; o.progress_seconds=std::stod(v,&pos);
      require(pos==v.size() && o.progress_seconds>=0 && o.progress_seconds<=86400,"invalid progress interval");
    } else {
      auto n=number(v); require(n<=1024,"worker/queue/test-delay option too large");
      auto u=static_cast<unsigned>(n);
      if (k=="--workers") o.workers=u;
      else if (k=="--decode-workers") o.decoders=u;
      else if (k=="--prefetch") o.prefetch=u;
      else if (k=="--queue-batches") o.queue_batches=u;
      else if (k=="--test-delay-first-block-ms") o.test_delay_first_block_ms=u;
      else throw std::runtime_error("unknown option: "+k);
    }
  }
  require(!o.input.empty()&&!o.output.empty(),"--input and --output are required");
  require(o.workers&&o.decoders&&o.prefetch&&o.queue_batches&&o.block_records,"sizes must be positive");
  require(o.block_records<=16777216,"block-records exceeds 16M memory safety limit");
  return o;
}

struct Cancelled : std::runtime_error { Cancelled():std::runtime_error("pipeline cancelled"){} };
template<class T> class Queue {
  std::mutex mutex_; std::condition_variable cv_; std::deque<T> values_;
  std::size_t limit_; bool closed_=false; std::atomic<bool>& cancelled_;
public:
  std::atomic<U64> high_water{0};
  Queue(std::size_t limit,std::atomic<bool>& cancel):limit_(limit),cancelled_(cancel){}
  void push(T value) {
    std::unique_lock lock(mutex_);
    while (values_.size()>=limit_&&!closed_&&!cancelled_) cv_.wait_for(lock,std::chrono::milliseconds(100));
    if (cancelled_) throw Cancelled();
    require(!closed_,"push to closed queue");
    values_.push_back(std::move(value)); high_water=std::max<U64>(high_water,values_.size()); cv_.notify_all();
  }
  bool pop(T& result) {
    std::unique_lock lock(mutex_);
    while (values_.empty()&&!closed_&&!cancelled_) cv_.wait_for(lock,std::chrono::milliseconds(100));
    if (cancelled_) throw Cancelled();
    if (values_.empty()) return false;
    result=std::move(values_.front()); values_.pop_front(); cv_.notify_all(); return true;
  }
  void close() { std::lock_guard lock(mutex_); closed_=true; cv_.notify_all(); }
};
struct File {
  int fd;
  explicit File(const std::string& path):fd(::open(path.c_str(),O_RDONLY|O_CLOEXEC)) {
    require(fd>=0,"cannot open input: "+std::string(std::strerror(errno)));
  }
  ~File(){::close(fd);}
  std::vector<unsigned char> read(U64 offset,std::size_t count) const {
    std::vector<unsigned char> bytes(count); std::size_t done=0;
    while (done<count) {
      auto n=::pread(fd,bytes.data()+done,count-done,static_cast<off_t>(offset+done));
      if (n<0 && errno==EINTR) continue;
      require(n>0,"input read failed or unexpected EOF"); done+=static_cast<std::size_t>(n);
    }
    return bytes;
  }
};
bool same_file(const struct stat& a,const struct stat& b) {
  return a.st_dev==b.st_dev&&a.st_ino==b.st_ino&&a.st_size==b.st_size
    &&a.st_mtim.tv_sec==b.st_mtim.tv_sec&&a.st_mtim.tv_nsec==b.st_mtim.tv_nsec
    &&a.st_ctim.tv_sec==b.st_ctim.tv_sec&&a.st_ctim.tv_nsec==b.st_ctim.tv_nsec;
}
struct Event { U64 line_write,timestamp,index; };
struct Batch { std::vector<Event> events; U64 base=0,origin=0; };
struct Block {
  std::vector<unsigned char> raw;
  std::vector<Batch> shards;
  U64 count=0,valid=0,first_time=0,last_time=0;
};
struct Pending { U64 timestamp,index; };
struct TimelineCounts {
  U64 reads=0,writes=0,matched=0;
  void merge(const TimelineCounts& b) {
    reads=add(reads,b.reads); writes=add(writes,b.writes); matched=add(matched,b.matched);
  }
};
using Timeline = std::map<U64,TimelineCounts>;
struct Worker {
  Queue<Batch> queue;
  std::array<Stats,2> stats;
  std::array<Timeline,2> timeline;
  U64 timeline_bin_cycles;
  std::atomic<U64> processed{0},current_pending{0},max_pending{0};
  Worker(unsigned cap,std::atomic<bool>& cancel,U64 bin_cycles):queue(cap,cancel),timeline_bin_cycles(bin_cycles){}
  void run() {
    std::unordered_map<U64,Pending,Hash> pending;
    Batch batch;
    std::array<U64,2> cached_bin{};
    std::array<TimelineCounts*,2> cached_counts{};
    while (queue.pop(batch)) {
      U64 peak=max_pending;
      for (const auto& e:batch.events) {
        const U64 key=e.line_write&~WRITE, index=batch.base+e.index;
        auto& s=stats[key&1];
        TimelineCounts* tc=nullptr;
        if (timeline_bin_cycles) {
          require(e.timestamp>=batch.origin,"timeline event precedes origin");
          const U64 bin=(e.timestamp-batch.origin)/timeline_bin_cycles;
          const auto channel=key&1;
          if (!cached_counts[channel] || cached_bin[channel]!=bin) {
            cached_counts[channel]=&timeline[channel][bin]; cached_bin[channel]=bin;
          }
          tc=cached_counts[channel];
        }
        if (e.line_write&WRITE) {
          ++s.writes;
          if (tc) ++tc->writes;
          auto [it,inserted]=pending.try_emplace(key,Pending{e.timestamp,index});
          if (!inserted) { ++s.superseded; it->second={e.timestamp,index}; }
          peak=std::max<U64>(peak,pending.size());
        } else {
          ++s.reads; auto it=pending.find(key);
          if (tc) ++tc->reads;
          if (it==pending.end()) ++s.unpaired_reads;
          else {
            require(e.timestamp>=it->second.timestamp && index>it->second.index,"pair order violated");
            ++s.matched; s.time.sample(e.timestamp-it->second.timestamp);
            if (tc) ++tc->matched;
            s.operations.sample(index-it->second.index-1); pending.erase(it);
          }
        }
      }
      processed.fetch_add(batch.events.size(),std::memory_order_relaxed);
      current_pending=pending.size(); max_pending=peak;
      batch.events.clear();
    }
    for (const auto& [key,p]:pending) { (void)p; ++stats[key&1].pending; }
    for (const auto& s:stats) s.verify();
  }
};

void atomic_write(const fs::path& path,const std::string& data) {
  auto tmp=path; tmp+=".tmp";
  std::ofstream out(tmp,std::ios::binary); out << data; out.flush();
  require(bool(out),"cannot write "+path.string()); out.close();
  require(bool(out),"cannot close "+path.string()); fs::rename(tmp,path);
}
std::string timeline_csv(const std::array<Timeline,3>& timelines,U64 width) {
  std::ostringstream o;
  o << "group,bin,start_offset_cycles,end_offset_cycles_exclusive,reads,writes,matched_pairs\n";
  const std::array<std::string,3> names={"combined","channel_0","channel_1"};
  for (const auto& [bin,counts]:timelines[0]) {
    (void)counts;
    for (unsigned g=0;g<3;++g) {
      const auto it=timelines[g].find(bin);
      const auto c=it==timelines[g].end()?TimelineCounts{}:it->second;
      o << names[g] << ',' << bin << ',' << decimal(U128(bin)*width) << ','
        << decimal((U128(bin)+1)*width) << ',' << c.reads << ',' << c.writes << ',' << c.matched << '\n';
    }
  }
  return o.str();
}
std::string hist_csv(const std::array<Stats,3>& groups,bool time) {
  std::ostringstream o; o << std::setprecision(18);
  o << "group,bin,lower_inclusive,upper_inclusive,count,fraction_of_matched_pairs\n";
  const std::array<std::string,3> names={"combined","channel_0","channel_1"};
  for (unsigned g=0;g<3;++g) {
    const auto& m=time?groups[g].time:groups[g].operations;
    for (unsigned i=0;i<65;++i) {
      auto [lo,hi]=bounds(i); o << names[g] << ',' << i << ',' << lo << ',' << hi << ',' << m.bins[i] << ',';
      if (m.count) o << static_cast<long double>(m.bins[i])/m.count;
      o << '\n';
    }
  }
  return o.str();
}
std::string summary_csv(const std::array<Stats,3>& groups) {
  std::ostringstream o; o << std::setprecision(18);
  o << "group,valid_accesses,reads,writes,matched_writes,superseded_writes,pending_writes_at_end,reads_without_pending_write,write_match_rate,zero_time_matches,zero_operation_matches,mean_time_cycles,mean_intervening_operations\n";
  const std::array<std::string,3> names={"combined","channel_0","channel_1"};
  for (unsigned i=0;i<3;++i) {
    const auto& s=groups[i];
    o << names[i] << ',' << add(s.reads,s.writes) << ',' << s.reads << ',' << s.writes << ',' << s.matched << ','
      << s.superseded << ',' << s.pending << ',' << s.unpaired_reads << ',';
    if (s.writes) o << static_cast<long double>(s.matched)/s.writes;
    o << ',' << s.time.bins[0] << ',' << s.operations.bins[0] << ',';
    if (s.matched) o << static_cast<long double>(s.time.sum)/s.matched;
    o << ','; if (s.matched) o << static_cast<long double>(s.operations.sum)/s.matched;
    o << '\n';
  }
  return o.str();
}

void analyze(const Options& o) {
  File input(o.input); struct stat before{};
  require(::fstat(input.fd,&before)==0 && S_ISREG(before.st_mode),"input must be a regular file");
  auto header=input.read(0,32);
  auto magic_version=load64(header.data());
  require((magic_version&0xffffffff)==0x54524143 && (magic_version>>32)==1,"invalid CART magic/version");
  U64 buffer_size=load64(header.data()+8),records=load64(header.data()+16),dropped=load64(header.data()+24);
  require(records<=(MAX64-32)/16,"CART count overflows file size");
  require(before.st_size>=0 && static_cast<U64>(before.st_size)==32+records*16,"CART header/file size mismatch");
  const U64 target=o.max_records?std::min(records,*o.max_records):records;
  const U64 block_count=target/o.block_records+(target%o.block_records!=0);
  std::unique_ptr<EVP_MD_CTX,decltype(&EVP_MD_CTX_free)> digest(EVP_MD_CTX_new(),EVP_MD_CTX_free);
  require(digest && EVP_DigestInit_ex(digest.get(),EVP_sha256(),nullptr)==1,"SHA256 init failed");
  require(EVP_DigestUpdate(digest.get(),header.data(),header.size())==1,"SHA256 update failed");
  std::atomic<bool> cancel{false}; std::mutex error_mutex; std::exception_ptr error;
  auto fail=[&](){ std::lock_guard lock(error_mutex); if (!error) error=std::current_exception(); cancel=true; };
  Queue<std::packaged_task<Block()>> jobs(o.prefetch,cancel);
  std::vector<std::unique_ptr<Worker>> workers;
  for (unsigned i=0;i<o.workers;++i) workers.push_back(std::make_unique<Worker>(o.queue_batches,cancel,o.timeline_bin_cycles));
  std::vector<std::thread> threads;
  std::jthread progress;
  std::atomic<U64> decoded_raw{0},dispatched_raw{0},dispatched_valid{0};
  const auto start=Clock::now();
  auto report=[&](bool final) {
    U64 processed=0,pending=0,peak=0,queue_peak=0;
    for (auto& w:workers) {
      processed+=w->processed.load(); pending+=w->current_pending.load(); peak+=w->max_pending.load();
      queue_peak+=w->queue.high_water.load();
    }
    double elapsed=std::chrono::duration<double>(Clock::now()-start).count();
    double rate=elapsed>0?static_cast<double>(dispatched_raw.load())/elapsed:0;
    struct rusage usage{}; getrusage(RUSAGE_SELF,&usage);
    std::cerr << std::fixed << std::setprecision(1) << (final?"[complete] ":"[progress] ")
      << "raw=" << dispatched_raw << '/' << target << " (" << (target?100.0*static_cast<double>(dispatched_raw)/static_cast<double>(target):100.0)
      << "%) decoded_raw=" << decoded_raw << " dispatched_valid=" << dispatched_valid << " processed_valid=" << processed
      << " Mslots/s=" << rate/1e6 << " elapsed_s=" << elapsed << " eta_s=" << (rate>0?static_cast<double>(target-dispatched_raw)/rate:0)
      << " pending=" << pending << " sum_worker_pending_high_water=" << peak << " sum_queue_high_water_batches=" << queue_peak
      << " rss_high_water_MiB=" << static_cast<double>(usage.ru_maxrss)/1024.0 << '\n';
  };
  U64 total_valid=0,total_invalid=0,first_time=0,last_time=0; bool have_time=false;
  try {
    for (auto& w:workers) threads.emplace_back([&,p=w.get()](){
      try {p->run();} catch (const Cancelled&) {} catch (...) {fail();}
    });
    for (unsigned i=0;i<o.decoders;++i) threads.emplace_back([&](){
      try { std::packaged_task<Block()> job; while(jobs.pop(job)) job(); }
      catch (const Cancelled&) {} catch (...) {fail();}
    });
    if (o.progress_seconds>0) progress=std::jthread([&](std::stop_token stop){
      auto last=Clock::now();
      while(!stop.stop_requested()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        if (std::chrono::duration<double>(Clock::now()-last).count()>=o.progress_seconds) {
          report(false); last=Clock::now();
        }
      }
    });
    auto schedule=[&](U64 b) {
      std::packaged_task<Block()> task([&,b](){
        if (b==0 && o.test_delay_first_block_ms) std::this_thread::sleep_for(std::chrono::milliseconds(o.test_delay_first_block_ms));
        Block block; U64 first=b*o.block_records;
        block.count=std::min(o.block_records,target-first);
        block.raw=input.read(32+first*16,static_cast<std::size_t>(block.count*16));
        block.shards.resize(o.workers);
        for (auto& shard:block.shards) shard.events.reserve(static_cast<std::size_t>(block.count/o.workers+64));
        for (U64 i=0;i<block.count;++i) {
          const auto* p=block.raw.data()+i*16; U64 low=load64(p),time=load64(p+8);
          if (!(low>>63)) continue;
          if (block.valid && time<block.last_time)
            throw std::runtime_error("CART timestamps decrease within block "+std::to_string(b));
          if (!block.valid) block.first_time=time;
          block.last_time=time; U64 key=(low&ADDRESS_MASK)>>6;
          block.shards[hash_line(key)%o.workers].events.push_back({key|((low>>62&1)?WRITE:0),time,block.valid++});
        }
        decoded_raw.fetch_add(block.count); return block;
      });
      auto future=task.get_future(); jobs.push(std::move(task)); return future;
    };
    std::deque<std::future<Block>> pending;
    U64 next=0;
    for (;next<std::min<U64>(block_count,o.prefetch);++next) pending.push_back(schedule(next));
    while (!pending.empty()) {
      Block b=pending.front().get(); pending.pop_front();
      if (cancel) throw Cancelled();
      if (b.valid) {
        require(!have_time||b.first_time>=last_time,"CART timestamps decrease across blocks");
        if (!have_time) first_time=b.first_time;
        have_time=true; last_time=b.last_time;
      }
      require(EVP_DigestUpdate(digest.get(),b.raw.data(),b.raw.size())==1,"SHA256 update failed");
      for (unsigned w=0;w<o.workers;++w) if (!b.shards[w].events.empty()) {
        b.shards[w].base=total_valid; b.shards[w].origin=first_time;
        workers[w]->queue.push(std::move(b.shards[w]));
      }
      total_valid=add(total_valid,b.valid); total_invalid=add(total_invalid,b.count-b.valid);
      dispatched_valid=total_valid; dispatched_raw.fetch_add(b.count);
      if (next<block_count) pending.push_back(schedule(next++));
    }
    jobs.close(); for (auto& w:workers) w->queue.close();
    for (auto& t:threads) t.join();
    if (progress.joinable()) { progress.request_stop(); progress.join(); }
    if (error) std::rethrow_exception(error);
  } catch (...) {
    auto original=std::current_exception(); cancel=true; jobs.close();
    for (auto& w:workers) w->queue.close();
    for (auto& t:threads) if (t.joinable()) t.join();
    if (progress.joinable()) {progress.request_stop(); progress.join();}
    if (error) std::rethrow_exception(error);
    std::rethrow_exception(original);
  }
  std::array<Stats,3> groups;
  std::array<Timeline,3> timelines;
  U64 processed=0,peak=0,queue_peak=0;
  for (auto& w:workers) {
    groups[1].merge(w->stats[0]); groups[2].merge(w->stats[1]);
    for (unsigned channel=0;channel<2;++channel)
      for (const auto& [bin,c]:w->timeline[channel]) timelines[channel+1][bin].merge(c);
    processed=add(processed,w->processed); peak=add(peak,w->max_pending); queue_peak=add(queue_peak,w->queue.high_water);
  }
  groups[0].merge(groups[1]); groups[0].merge(groups[2]);
  for (const auto& g:groups) g.verify();
  if (o.timeline_bin_cycles) {
    for (unsigned g=1;g<3;++g)
      for (const auto& [bin,c]:timelines[g]) timelines[0][bin].merge(c);
    for (unsigned g=0;g<3;++g) {
      TimelineCounts total;
      for (const auto& [bin,c]:timelines[g]) {
        require(have_time && bin<=(last_time-first_time)/o.timeline_bin_cycles,"timeline bin out of range");
        require(c.matched<=c.reads,"timeline matches exceed reads"); total.merge(c);
      }
      require(total.reads==groups[g].reads && total.writes==groups[g].writes
              && total.matched==groups[g].matched,"timeline conservation failure");
    }
  }
  require(processed==total_valid && add(groups[0].reads,groups[0].writes)==total_valid,"valid event conservation failure");
  require(add(total_valid,total_invalid)==target,"raw slot conservation failure");
  struct stat after{},path_after{};
  require(::fstat(input.fd,&after)==0 && ::stat(o.input.c_str(),&path_after)==0
    && same_file(before,after) && same_file(before,path_after),"input changed during analysis");
  std::array<unsigned char,EVP_MAX_MD_SIZE> hash{}; unsigned hash_len=0;
  require(EVP_DigestFinal_ex(digest.get(),hash.data(),&hash_len)==1,"SHA256 final failed");
  std::ostringstream sha; sha << std::hex << std::setfill('0');
  for (unsigned i=0;i<hash_len;++i) sha << std::setw(2) << unsigned(hash[i]);
  struct rusage usage{}; getrusage(RUSAGE_SELF,&usage);
  std::ostringstream json; json << std::setprecision(18);
  json << "{\n\"schema_version\":1,\"analysis\":\"latest_write_first_read_64B\",\"full_trace\":" << (o.max_records?"false":"true")
    << ",\"timestamp_mhz\":400,\"input\":{\"path\":" << quoted(fs::absolute(o.input).string())
    << ",\"size_bytes\":" << before.st_size << ",\"device\":" << before.st_dev << ",\"inode\":" << before.st_ino
    << ",\"mtime_ns\":" << decimal(U128(before.st_mtim.tv_sec)*1000000000+before.st_mtim.tv_nsec)
    << ",\"buffer_size\":" << buffer_size << ",\"written_records\":" << records << ",\"dropped_records\":" << dropped
    << ",\"sha256\":" << quoted(sha.str()) << ",\"digest_scope\":" << quoted(o.max_records?"header_and_analyzed_prefix":"full_file")
    << "},\n\"options\":{\"workers\":" << o.workers << ",\"decode_workers\":" << o.decoders
    << ",\"block_records\":" << o.block_records << ",\"prefetch\":" << o.prefetch << ",\"queue_batches\":" << o.queue_batches
    << ",\"timeline_bin_cycles\":" << o.timeline_bin_cycles
    << ",\"max_records\":" << (o.max_records?std::to_string(*o.max_records):"null")
    << "},\n\"raw_record_slots\":" << target << ",\"invalid_record_slots\":" << total_invalid
    << ",\"performance\":{\"elapsed_seconds\":" << std::chrono::duration<double>(Clock::now()-start).count()
    << ",\"max_rss_kib\":" << usage.ru_maxrss << ",\"sum_worker_pending_high_water\":" << peak
    << ",\"sum_worker_queue_high_water_batches\":" << queue_peak << "},\n\"groups\":{\"combined\":" << groups[0].json()
    << ",\"channel_0\":" << groups[1].json() << ",\"channel_1\":" << groups[2].json() << "},\n\"timeline\":";
  if (o.timeline_bin_cycles) {
    json << "{\"schema_version\":1,\"bin_cycles\":" << o.timeline_bin_cycles
      << ",\"origin_timestamp\":" << (have_time?std::to_string(first_time):"null")
      << ",\"last_timestamp\":" << (have_time?std::to_string(last_time):"null")
      << ",\"last_bin\":" << (have_time?std::to_string((last_time-first_time)/o.timeline_bin_cycles):"null")
      << ",\"attribution\":\"matched_read_request\",\"storage\":\"sparse_event_bins\",\"empty_bins_are_zero\":true"
      << ",\"csv\":\"execution_timeline.csv\"}";
    atomic_write(fs::path(o.output)/"execution_timeline.csv",timeline_csv(timelines,o.timeline_bin_cycles));
  } else json << "null";
  json << "\n}\n";
  atomic_write(fs::path(o.output)/"summary.json",json.str());
  atomic_write(fs::path(o.output)/"summary.csv",summary_csv(groups));
  atomic_write(fs::path(o.output)/"time_histogram.csv",hist_csv(groups,true));
  atomic_write(fs::path(o.output)/"intervening_operations_histogram.csv",hist_csv(groups,false));
  atomic_write(fs::path(o.output)/(o.max_records?"SMOKE_COMPLETE":"COMPLETE"),"all conservation checks passed\n");
  report(true);
}

int main(int argc,char** argv) {
  fs::path output; bool owned=false;
  try {
    auto o=options(argc,argv); output=o.output;
    require(!fs::exists(output),"output already exists: "+output.string());
    fs::create_directories(output.parent_path().empty()?fs::path("."):output.parent_path());
    owned=fs::create_directory(output); require(owned,"cannot create exclusive output directory");
    analyze(o); return 0;
  } catch (const std::exception& e) {
    std::cerr << "error: " << e.what() << '\n';
    if (owned) try { atomic_write(output/"FAILED.json","{\"error\":"+quoted(e.what())+"}\n"); } catch (...) {}
    return 1;
  }
}
