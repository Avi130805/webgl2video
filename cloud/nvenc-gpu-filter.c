/*
 * nvenc-gpu-filter.c: an LD_PRELOAD shim that lets NVENC work in a container on a multi-GPU host.
 *
 * The bug (NVIDIA drivers 565 and later; nvidia-container-toolkit#1249, k8s-device-plugin#1282): a container given one
 * GPU of a multi-GPU host still gets the host's whole list of GPUs from the driver's resource manager (the RM control
 * NV0000_CTRL_CMD_GPU_GET_ATTACHED_IDS). NVENC then reaches for GPUs whose /dev/nvidiaN nodes aren't mounted and fails
 * with NV_ENC_ERR_UNSUPPORTED_DEVICE ("OpenEncodeSessionEx failed: unsupported device (2)"), unless the mounted GPU
 * happens to be the host's last one.
 *
 * The fix: after the real ioctl answers that one query, each GPU id in the answer is located (PCI domain, bus, device,
 * asked of the driver with NV0000_CTRL_CMD_GPU_GET_PCI_INFO) and kept only if /proc/driver/nvidia/gpus/<bus id>/
 * information names a "Device Minor" whose /dev/nvidia<minor> exists in this container. Every other ioctl passes
 * through untouched. Fail-safe: when anything can't be resolved, or nothing (or everything) would be kept, the driver's
 * answer is returned exactly as it was.
 *
 * Build:  gcc -O2 -shared -fPIC -o nvenc-gpu-filter.so nvenc-gpu-filter.c -ldl -lpthread
 * Use:    LD_PRELOAD=/path/to/nvenc-gpu-filter.so ffmpeg ... -c:v hevc_nvenc ...
 *         NVENC_FILTER_DEBUG=1 prints what it sees and decides, on stderr.
 * Test:   cc -DNVENC_FILTER_TEST -o nvenc-filter-test nvenc-gpu-filter.c -lpthread && ./nvenc-filter-test
 *         (no GPU needed: a mock driver and a mock /dev and /proc, on any OS)
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* The fields of a Linux ioctl request number (asm-generic layout), spelled out so the test builds anywhere. */
#define REQ_NR(r) ((unsigned)((r) & 0xffu))
#define REQ_TYPE(r) ((unsigned)(((r) >> 8) & 0xffu))
#define REQ_SIZE(r) ((unsigned)(((r) >> 16) & 0x3fffu))
#define REQ_MAKE(dir, type, nr, size) \
    ((unsigned long)(((unsigned long)(dir) << 30) | ((unsigned long)(size) << 16) | ((unsigned long)(type) << 8) | (unsigned long)(nr)))
#define REQ_READ_WRITE 3u

/* The NVIDIA driver's ioctl interface (open-gpu-kernel-modules: nv-ioctl-numbers.h, nvos.h, ctrl0000gpu.h). */
#define NV_IOCTL_MAGIC 'F'
#define NV_ESC_RM_CONTROL 0x2Au
#define NV_ESC_IOCTL_XFER_CMD 0xD3u /* NV_IOCTL_BASE (200) + 11: a wrapper for large escapes */
#define CMD_GPU_GET_ATTACHED_IDS 0x201u
#define CMD_GPU_GET_PCI_INFO 0x21Bu
#define MAX_ATTACHED 32
#define INVALID_ID 0xffffffffu

typedef struct { /* NVOS54_PARAMETERS, 32 bytes */
    uint32_t hClient, hObject, cmd, flags;
    uint64_t params; /* NvP64: a pointer to the control's own parameters */
    uint32_t paramsSize, status;
} rm_control_t;
typedef struct { uint32_t cmd, size; uint64_t ptr; } xfer_t; /* nv_ioctl_xfer_t */
typedef struct { uint32_t gpuId, domain; uint16_t bus, slot; } pci_info_t; /* NV0000_CTRL_GPU_GET_PCI_INFO_PARAMS */

typedef int (*ioctl_fn)(int, unsigned long, void *);
static ioctl_fn next_ioctl; /* the real ioctl (the mock driver in the test) */
static int debug_on;
#define DBG(...) do { if (debug_on) fprintf(stderr, "[nvenc-gpu-filter] " __VA_ARGS__); } while (0)

/* The GPUs this container can open: PCI location of each one whose device node is mounted here. */
static struct { unsigned domain, bus, dev; } allowed[MAX_ATTACHED];
static int n_allowed;
#ifndef NVENC_FILTER_TEST
static pthread_once_t loaded = PTHREAD_ONCE_INIT;
#endif

static const char *env_or(const char *k, const char *d) {
    const char *v = getenv(k);
    return v && *v ? v : d;
}

static void load_allowed(void) {
    debug_on = getenv("NVENC_FILTER_DEBUG") != NULL;
    n_allowed = 0;
    const char *devdir = env_or("NVENC_FILTER_DEV_DIR", "/dev");
    const char *gpudir = env_or("NVENC_FILTER_GPUS_DIR", "/proc/driver/nvidia/gpus");
    DIR *g = opendir(gpudir);
    if (!g) { DBG("no %s: answers left as they are\n", gpudir); return; }
    struct dirent *e;
    while ((e = readdir(g)) != NULL && n_allowed < MAX_ATTACHED) {
        if (e->d_name[0] == '.') continue;
        char path[1024];
        snprintf(path, sizeof path, "%s/%s/information", gpudir, e->d_name);
        FILE *f = fopen(path, "r");
        if (!f) continue;
        char line[512];
        unsigned dom = 0, bus = 0, dev = 0, fn = 0;
        int minor = -1, have_bus = 0;
        while (fgets(line, sizeof line, f)) {
            char *v = strchr(line, ':'); /* "Bus Location: \t 0000:41:00.0", "Device Minor: \t 1" */
            if (!v) continue;
            if (!strncmp(line, "Bus Location", 12) && sscanf(v + 1, " %x:%x:%x.%x", &dom, &bus, &dev, &fn) == 4) have_bus = 1;
            else if (!strncmp(line, "Device Minor", 12)) minor = atoi(v + 1);
        }
        fclose(f);
        if (!have_bus || minor < 0) continue;
        char node[1024];
        snprintf(node, sizeof node, "%s/nvidia%d", devdir, minor);
        int here = access(node, F_OK) == 0;
        DBG("GPU %04x:%02x:%02x.%x, minor %d: %s\n", dom, bus, dev, fn, minor, here ? "mounted here" : "not mounted");
        if (here) {
            allowed[n_allowed].domain = dom;
            allowed[n_allowed].bus = bus;
            allowed[n_allowed].dev = dev;
            n_allowed++;
        }
    }
    closedir(g);
}

/* Where is this GPU? Asked of the driver on the same handle; failing that, the RM's own id scheme. */
static void locate(int fd, const rm_control_t *orig, uint32_t id, unsigned *dom, unsigned *bus, unsigned *dev) {
    pci_info_t pi;
    memset(&pi, 0, sizeof pi);
    pi.gpuId = id;
    rm_control_t q;
    memset(&q, 0, sizeof q);
    q.hClient = orig->hClient;
    q.hObject = orig->hClient; /* NV0000 controls go to the client object itself */
    q.cmd = CMD_GPU_GET_PCI_INFO;
    q.params = (uint64_t)(uintptr_t)&pi;
    q.paramsSize = sizeof pi;
    if (next_ioctl(fd, REQ_MAKE(REQ_READ_WRITE, NV_IOCTL_MAGIC, NV_ESC_RM_CONTROL, sizeof q), &q) == 0 && q.status == 0) {
        *dom = pi.domain; *bus = pi.bus; *dev = pi.slot;
        return;
    }
    *dom = id >> 16; *bus = (id >> 8) & 0xffu; *dev = id & 0xffu; /* (domain << 16) | (bus << 8) | device */
    DBG("GET_PCI_INFO refused for id 0x%08x (status 0x%x): reading the id's own encoding\n", id, q.status);
}

static int is_allowed(unsigned dom, unsigned bus, unsigned dev) {
    for (int a = 0; a < n_allowed; a++)
        if (allowed[a].domain == dom && allowed[a].bus == bus && allowed[a].dev == dev) return 1;
    return 0;
}

#ifdef NVENC_FILTER_TEST
static int test_reload = 1; /* the test changes the mock /dev and /proc between cases */
static void ensure_loaded(void) { if (test_reload) { test_reload = 0; load_allowed(); } }
#else
static void ensure_loaded(void) { pthread_once(&loaded, load_allowed); }
#endif

static void filter_attached(int fd, rm_control_t *p) {
    ensure_loaded();
    if (n_allowed == 0) return;
    uint32_t *ids = (uint32_t *)(uintptr_t)p->params;
    uint32_t keep[MAX_ATTACHED];
    int k = 0, total = 0;
    for (int i = 0; i < MAX_ATTACHED; i++) {
        uint32_t id = ids[i];
        if (id == INVALID_ID) continue;
        total++;
        unsigned dom, bus, dev;
        locate(fd, p, id, &dom, &bus, &dev);
        int ok = is_allowed(dom, bus, dev);
        DBG("attached GPU id 0x%08x at %04x:%02x:%02x: %s\n", id, dom, bus, dev, ok ? "kept" : "dropped (not mounted here)");
        if (ok) keep[k++] = id;
    }
    if (k == 0 || k == total) { DBG("%d of %d kept: answer left as it was\n", k, total); return; }
    for (int i = 0; i < MAX_ATTACHED; i++) ids[i] = i < k ? keep[i] : INVALID_ID;
    DBG("answer filtered to %d of %d GPUs\n", k, total);
}

#ifdef NVENC_FILTER_TEST
static int is_nvidiactl(int fd) { (void)fd; return 1; }
#else
static int is_nvidiactl(int fd) { /* only the driver's control node: other devices also use 'F' ioctls */
    char link[64], target[64];
    snprintf(link, sizeof link, "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, target, sizeof target - 1);
    if (n <= 0) return 0;
    target[n] = 0;
    return strcmp(target, "/dev/nvidiactl") == 0;
}
#endif

/* After a successful ioctl: was it the attached-GPU query? (directly, or inside the driver's transfer wrapper) */
static void after_ioctl(int fd, unsigned long request, void *arg) {
    if (!arg || REQ_TYPE(request) != NV_IOCTL_MAGIC) return;
    rm_control_t *p = NULL;
    if (REQ_NR(request) == NV_ESC_RM_CONTROL && REQ_SIZE(request) >= sizeof(rm_control_t)) {
        p = (rm_control_t *)arg;
    } else if (REQ_NR(request) == NV_ESC_IOCTL_XFER_CMD && REQ_SIZE(request) >= sizeof(xfer_t)) {
        xfer_t *x = (xfer_t *)arg;
        if (x->cmd == NV_ESC_RM_CONTROL && x->size >= sizeof(rm_control_t) && x->ptr) p = (rm_control_t *)(uintptr_t)x->ptr;
    }
    if (p && p->cmd == CMD_GPU_GET_ATTACHED_IDS && p->status == 0 && p->params &&
        p->paramsSize >= MAX_ATTACHED * sizeof(uint32_t) && is_nvidiactl(fd))
        filter_attached(fd, p);
}

static int hooked_ioctl(int fd, unsigned long request, void *arg) {
    int ret = next_ioctl(fd, request, arg);
    if (ret == 0) after_ioctl(fd, request, arg);
    return ret;
}

#ifndef NVENC_FILTER_TEST
/* glibc: int ioctl(int, unsigned long, ...). The third argument is forwarded as the pointer-sized value it was given. */
int ioctl(int fd, unsigned long request, ...) {
    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    if (!next_ioctl) next_ioctl = (ioctl_fn)dlsym(RTLD_NEXT, "ioctl");
    if (!next_ioctl) return -1;
    return hooked_ioctl(fd, request, arg);
}
#else
/* ---- the test: a mock driver with three GPUs, of which this "container" mounts only some ------------------------ */
#include <sys/stat.h>
static uint32_t host_ids[3] = {0x00000100u, 0x00004100u, 0x00008100u}; /* 0000:01:00, 0000:41:00, 0000:81:00 */
static int pci_info_works = 1, pci_calls = 0;

static int mock_driver(int fd, unsigned long request, void *arg) {
    (void)fd;
    rm_control_t *p = NULL;
    if (REQ_NR(request) == NV_ESC_RM_CONTROL) p = arg;
    else if (REQ_NR(request) == NV_ESC_IOCTL_XFER_CMD) p = (rm_control_t *)(uintptr_t)((xfer_t *)arg)->ptr;
    if (!p) return -1;
    if (p->cmd == CMD_GPU_GET_ATTACHED_IDS) {
        uint32_t *ids = (uint32_t *)(uintptr_t)p->params;
        for (int i = 0; i < MAX_ATTACHED; i++) ids[i] = i < 3 ? host_ids[i] : INVALID_ID;
        p->status = 0;
    } else if (p->cmd == CMD_GPU_GET_PCI_INFO) {
        pci_calls++;
        pci_info_t *pi = (pci_info_t *)(uintptr_t)p->params;
        if (!pci_info_works) { p->status = 0x56; return 0; } /* NV_ERR_NOT_SUPPORTED */
        pi->domain = pi->gpuId >> 16; pi->bus = (pi->gpuId >> 8) & 0xff; pi->slot = pi->gpuId & 0xff;
        p->status = 0;
    }
    return 0;
}

static char root[256];
static void write_gpu(const char *bus_id, int minor) {
    char d[512], f[600];
    snprintf(d, sizeof d, "%s/gpus/%s", root, bus_id);
    mkdir(d, 0755);
    snprintf(f, sizeof f, "%s/information", d);
    FILE *fp = fopen(f, "w");
    fprintf(fp, "Model: \t\t NVIDIA GeForce RTX 4090\nBus Type: \t PCIe\nBus Location: \t %s\nDevice Minor: \t %d\nGPU Excluded:\t No\n", bus_id, minor);
    fclose(fp);
}
static void mount_minors(int a, int b, int c) {
    char n[512];
    for (int m = 0; m < 3; m++) {
        snprintf(n, sizeof n, "%s/dev/nvidia%d", root, m);
        int want = (m == 0 && a) || (m == 1 && b) || (m == 2 && c);
        if (want) { FILE *fp = fopen(n, "w"); if (fp) fclose(fp); } else unlink(n);
    }
}
static int query(int use_xfer, uint32_t *out) {
    uint32_t ids[MAX_ATTACHED];
    rm_control_t p;
    memset(&p, 0, sizeof p);
    p.hClient = p.hObject = 0xc1d00001u;
    p.cmd = CMD_GPU_GET_ATTACHED_IDS;
    p.params = (uint64_t)(uintptr_t)ids;
    p.paramsSize = sizeof ids;
    test_reload = 1; /* reload the mock /dev and /proc for each case */
    if (use_xfer) {
        xfer_t x = {NV_ESC_RM_CONTROL, sizeof p, (uint64_t)(uintptr_t)&p};
        hooked_ioctl(3, REQ_MAKE(REQ_READ_WRITE, NV_IOCTL_MAGIC, NV_ESC_IOCTL_XFER_CMD, sizeof x), &x);
    } else {
        hooked_ioctl(3, REQ_MAKE(REQ_READ_WRITE, NV_IOCTL_MAGIC, NV_ESC_RM_CONTROL, sizeof p), &p);
    }
    int n = 0;
    for (int i = 0; i < MAX_ATTACHED; i++) if (ids[i] != INVALID_ID) out[n++] = ids[i];
    return n;
}
static int fails = 0;
static void expect(const char *name, int n, const uint32_t *got, int want_n, const uint32_t *want) {
    int ok = n == want_n;
    for (int i = 0; ok && i < n; i++) ok = got[i] == want[i];
    printf("%s %s (%d GPUs:", ok ? "PASS" : "FAIL", name, n);
    for (int i = 0; i < n; i++) printf(" 0x%x", got[i]);
    printf(")\n");
    fails += !ok;
}

int main(void) {
    next_ioctl = mock_driver;
    snprintf(root, sizeof root, "/tmp/nvenc-filter-test-%d", (int)getpid());
    char d[512];
    mkdir(root, 0755);
    snprintf(d, sizeof d, "%s/gpus", root); mkdir(d, 0755);
    snprintf(d, sizeof d, "%s/dev", root); mkdir(d, 0755);
    snprintf(d, sizeof d, "%s/gpus", root); setenv("NVENC_FILTER_GPUS_DIR", d, 1);
    snprintf(d, sizeof d, "%s/dev", root); setenv("NVENC_FILTER_DEV_DIR", d, 1);
    if (getenv("VERBOSE")) setenv("NVENC_FILTER_DEBUG", "1", 1);
    write_gpu("0000:01:00.0", 0);
    write_gpu("0000:41:00.0", 1);
    write_gpu("0000:81:00.0", 2);
    uint32_t got[MAX_ATTACHED];
    int n;
    uint32_t only_first[] = {0x100}, only_mid[] = {0x4100}, all[] = {0x100, 0x4100, 0x8100}, first_last[] = {0x100, 0x8100};

    mount_minors(0, 1, 0); n = query(0, got); expect("one GPU mounted (minor 1): only it is listed", n, got, 1, only_mid);
    mount_minors(1, 0, 0); n = query(0, got); expect("one GPU mounted (minor 0): only it is listed", n, got, 1, only_first);
    mount_minors(0, 1, 0); n = query(1, got); expect("the same through the driver's transfer wrapper", n, got, 1, only_mid);
    mount_minors(1, 0, 1); n = query(0, got); expect("two of three mounted: both listed, in order", n, got, 2, first_last);
    mount_minors(1, 1, 1); n = query(0, got); expect("all mounted: answer untouched", n, got, 3, all);
    mount_minors(0, 0, 0); n = query(0, got); expect("none mounted: answer untouched (fail-safe)", n, got, 3, all);
    pci_info_works = 0;
    mount_minors(0, 1, 0); n = query(0, got); expect("GET_PCI_INFO refused: the id's own encoding still matches", n, got, 1, only_mid);
    pci_info_works = 1;
    host_ids[1] = 0x00004200u; /* a GPU the /proc listing doesn't know */
    mount_minors(0, 1, 0); n = query(0, got);
    uint32_t odd[] = {0x100, 0x4200, 0x8100};
    expect("unknown location: answer untouched (fail-safe)", n, got, 3, odd);
    host_ids[1] = 0x00004100u;
    setenv("NVENC_FILTER_GPUS_DIR", "/nonexistent", 1);
    mount_minors(0, 1, 0); n = query(0, got); expect("no /proc listing: answer untouched (fail-safe)", n, got, 3, all);
    {   /* other ioctls pass through untouched */
        int before = pci_calls;
        rm_control_t p; uint32_t ids[MAX_ATTACHED];
        memset(&p, 0, sizeof p); p.cmd = 0x202u; p.params = (uint64_t)(uintptr_t)ids; p.paramsSize = sizeof ids;
        hooked_ioctl(3, REQ_MAKE(REQ_READ_WRITE, NV_IOCTL_MAGIC, NV_ESC_RM_CONTROL, sizeof p), &p);
        hooked_ioctl(3, REQ_MAKE(REQ_READ_WRITE, 'T', 0x2A, sizeof p), &p);
        int ok = pci_calls == before;
        printf("%s other controls and other devices pass through untouched\n", ok ? "PASS" : "FAIL");
        fails += !ok;
    }
    char cmd[600];
    snprintf(cmd, sizeof cmd, "rm -rf '%s'", root);
    if (system(cmd) != 0) fprintf(stderr, "could not remove %s\n", root);
    printf(fails ? "%d FAILED\n" : "all passed\n", fails);
    return fails != 0;
}
#endif
