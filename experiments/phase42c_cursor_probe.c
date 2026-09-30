/*
 * Phase 4.2C isolated host-only cursor metadata composition probe.
 *
 * This is deliberately not production code. It consumes an XDG portal
 * PipeWire remote supplied by phase42c_portal.py, keeps desktop pixels and
 * SPA cursor metadata in separate caches, and publishes a clock-driven 30 Hz
 * local preview through GStreamer.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
#include <pipewire/pipewire.h>
#include <spa/buffer/meta.h>
#include <spa/param/buffers.h>
#include <spa/param/format-utils.h>
#include <spa/param/video/format-utils.h>
#include <spa/param/video/raw.h>
#include <spa/utils/result.h>

#define CURSOR_MAX 384
#define CURSOR_META_SIZE(w, h) \
  (sizeof(struct spa_meta_cursor) + sizeof(struct spa_meta_bitmap) + (w) * (h) * 4)

struct cursor_cache {
  bool visible;
  bool have_bitmap;
  int32_t x, y;
  int32_t hotspot_x, hotspot_y;
  uint32_t width, height, format;
  int32_t stride;
  uint8_t pixels[CURSOR_MAX * CURSOR_MAX * 4];
  uint64_t generation;
  uint64_t received_ns;
  uint64_t bitmap_hash;
};

struct app {
  uint32_t width, height, fps;
  int remote_fd;
  uint32_t node_id;

  struct pw_main_loop *loop;
  struct pw_context *context;
  struct pw_core *core;
  struct pw_stream *stream;
  struct spa_hook stream_listener;

  pthread_mutex_t lock;
  uint8_t *desktop;
  bool have_desktop;
  struct cursor_cache cursor;

  pthread_t output_thread;
  bool output_started;
  volatile sig_atomic_t stopping;
  GstElement *preview_pipeline;
  GstAppSrc *preview_src;

  uint64_t started_ns;
  uint64_t source_buffers;
  uint64_t video_frames;
  uint64_t cursor_events;
  uint64_t cursor_position_events;
  uint64_t cursor_bitmap_events;
  uint64_t cursor_hidden_events;
  uint64_t distinct_shapes;
  uint64_t last_shape_hash;
  uint64_t output_frames;
  uint64_t max_output_gap_ns;
  uint64_t last_output_ns;
  uint64_t last_presented_cursor_generation;
  uint64_t cursor_latency_samples;
  uint64_t cursor_latency_sum_ns;
  uint64_t cursor_latency_max_ns;
  uint64_t out_of_bounds_positions;
};

static struct app *global_app;

static uint64_t mono_ns(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static uint64_t fnv1a(const uint8_t *data, size_t size) {
  uint64_t hash = 1469598103934665603ull;
  for (size_t i = 0; i < size; i++) {
    hash ^= data[i];
    hash *= 1099511628211ull;
  }
  return hash;
}

static void request_stop(struct app *app) {
  if (!app->stopping) {
    app->stopping = 1;
    if (app->loop)
      pw_main_loop_quit(app->loop);
  }
}

static void signal_handler(int signo) {
  (void)signo;
  if (global_app)
    request_stop(global_app);
}

static void print_metrics(struct app *app, const char *kind) {
  uint64_t now = mono_ns();
  double seconds = (double)(now - app->started_ns) / 1e9;
  if (seconds <= 0.0)
    seconds = 0.001;
  pthread_mutex_lock(&app->lock);
  struct cursor_cache cursor = app->cursor;
  fprintf(stderr,
          "METRIC kind=%s elapsed=%.3f source_buffers=%" PRIu64
          " source_buffer_fps=%.3f video_frames=%" PRIu64
          " video_fps=%.3f cursor_events=%" PRIu64
          " cursor_fps=%.3f output_frames=%" PRIu64
          " output_fps=%.3f max_output_gap_ms=%.3f cursor_visible=%d"
          " cursor_x=%d cursor_y=%d hotspot_x=%d hotspot_y=%d"
          " cursor_width=%u cursor_height=%u cursor_format=%u"
          " bitmap_events=%" PRIu64 " hidden_events=%" PRIu64
          " distinct_shapes=%" PRIu64 " out_of_bounds=%" PRIu64
          " cursor_latency_avg_ms=%.3f cursor_latency_max_ms=%.3f\n",
          kind, seconds, app->source_buffers, app->source_buffers / seconds,
          app->video_frames, app->video_frames / seconds,
          app->cursor_events, app->cursor_events / seconds,
          app->output_frames, app->output_frames / seconds,
          app->max_output_gap_ns / 1e6, cursor.visible, cursor.x, cursor.y,
          cursor.hotspot_x, cursor.hotspot_y, cursor.width, cursor.height,
          cursor.format, app->cursor_bitmap_events, app->cursor_hidden_events,
          app->distinct_shapes, app->out_of_bounds_positions,
          app->cursor_latency_samples ?
            (app->cursor_latency_sum_ns / (double)app->cursor_latency_samples) / 1e6 : 0.0,
          app->cursor_latency_max_ns / 1e6);
  fflush(stderr);
  pthread_mutex_unlock(&app->lock);
}

static inline void blend_rgba_on_bgrx(uint8_t *dst, const uint8_t *src) {
  uint32_t a = src[3];
  uint32_t inv = 255u - a;
  dst[0] = (uint8_t)((src[2] * a + dst[0] * inv + 127u) / 255u);
  dst[1] = (uint8_t)((src[1] * a + dst[1] * inv + 127u) / 255u);
  dst[2] = (uint8_t)((src[0] * a + dst[2] * inv + 127u) / 255u);
  dst[3] = 255;
}

static void composite_cursor(struct app *app, uint8_t *frame,
                             const struct cursor_cache *cursor) {
  if (!cursor->visible || !cursor->have_bitmap || cursor->format != SPA_VIDEO_FORMAT_RGBA)
    return;
  int32_t left = cursor->x - cursor->hotspot_x;
  int32_t top = cursor->y - cursor->hotspot_y;
  for (uint32_t cy = 0; cy < cursor->height; cy++) {
    int32_t dy = top + (int32_t)cy;
    if (dy < 0 || dy >= (int32_t)app->height)
      continue;
    const uint8_t *src_row = cursor->pixels + (size_t)cy * (size_t)cursor->stride;
    for (uint32_t cx = 0; cx < cursor->width; cx++) {
      int32_t dx = left + (int32_t)cx;
      if (dx < 0 || dx >= (int32_t)app->width)
        continue;
      uint8_t *dst = frame + ((size_t)dy * app->width + (uint32_t)dx) * 4;
      blend_rgba_on_bgrx(dst, src_row + (size_t)cx * 4);
    }
  }
}

static void *output_main(void *opaque) {
  struct app *app = opaque;
  const uint64_t interval = 1000000000ull / app->fps;
  uint64_t deadline = mono_ns();
  uint64_t next_report = deadline + 1000000000ull;
  size_t frame_size = (size_t)app->width * app->height * 4;

  while (!app->stopping) {
    deadline += interval;
    struct timespec ts = { .tv_sec = deadline / 1000000000ull,
                           .tv_nsec = deadline % 1000000000ull };
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR && !app->stopping) {}
    if (app->stopping)
      break;

    GstBuffer *buffer = gst_buffer_new_allocate(NULL, frame_size, NULL);
    GstMapInfo map;
    if (!buffer || !gst_buffer_map(buffer, &map, GST_MAP_WRITE)) {
      fprintf(stderr, "ERROR output buffer allocation/map failed\n");
      if (buffer)
        gst_buffer_unref(buffer);
      request_stop(app);
      break;
    }

    struct cursor_cache cursor;
    bool have_desktop;
    pthread_mutex_lock(&app->lock);
    have_desktop = app->have_desktop;
    if (have_desktop)
      memcpy(map.data, app->desktop, frame_size);
    else
      memset(map.data, 0, frame_size);
    cursor = app->cursor;
    pthread_mutex_unlock(&app->lock);

    composite_cursor(app, map.data, &cursor);
    gst_buffer_unmap(buffer, &map);
    GST_BUFFER_PTS(buffer) = app->output_frames * GST_SECOND / app->fps;
    GST_BUFFER_DURATION(buffer) = GST_SECOND / app->fps;

    GstFlowReturn flow = gst_app_src_push_buffer(app->preview_src, buffer);
    uint64_t now = mono_ns();
    pthread_mutex_lock(&app->lock);
    if (app->last_output_ns) {
      uint64_t gap = now - app->last_output_ns;
      if (gap > app->max_output_gap_ns)
        app->max_output_gap_ns = gap;
    }
    app->last_output_ns = now;
    app->output_frames++;
    if (cursor.generation != app->last_presented_cursor_generation) {
      if (cursor.received_ns && now >= cursor.received_ns) {
        uint64_t latency = now - cursor.received_ns;
        app->cursor_latency_samples++;
        app->cursor_latency_sum_ns += latency;
        if (latency > app->cursor_latency_max_ns)
          app->cursor_latency_max_ns = latency;
      }
      app->last_presented_cursor_generation = cursor.generation;
    }
    pthread_mutex_unlock(&app->lock);

    if (flow != GST_FLOW_OK) {
      fprintf(stderr, "ERROR preview appsrc flow=%s\n", gst_flow_get_name(flow));
      request_stop(app);
      break;
    }
    if (now >= next_report) {
      print_metrics(app, "periodic");
      next_report += 1000000000ull;
    }
  }
  gst_app_src_end_of_stream(app->preview_src);
  return NULL;
}

static void on_process(void *opaque) {
  struct app *app = opaque;
  struct pw_buffer *buffer = NULL;
  struct pw_buffer *next;
  while ((next = pw_stream_dequeue_buffer(app->stream)) != NULL) {
    if (buffer)
      pw_stream_queue_buffer(app->stream, buffer);
    buffer = next;
  }
  if (!buffer)
    return;

  struct spa_buffer *spa = buffer->buffer;
  struct spa_data *data = spa->n_datas ? &spa->datas[0] : NULL;
  struct spa_meta_cursor *meta = spa_buffer_find_meta_data(
      spa, SPA_META_Cursor, sizeof(struct spa_meta_cursor));
  uint64_t now = mono_ns();

  pthread_mutex_lock(&app->lock);
  app->source_buffers++;

  if (data && data->data && data->chunk && data->chunk->size > 0 &&
      !(data->chunk->flags & SPA_CHUNK_FLAG_CORRUPTED)) {
    uint32_t stride = data->chunk->stride > 0 ? (uint32_t)data->chunk->stride : app->width * 4;
    const uint8_t *src = (const uint8_t *)data->data + data->chunk->offset;
    for (uint32_t y = 0; y < app->height; y++)
      memcpy(app->desktop + (size_t)y * app->width * 4,
             src + (size_t)y * stride, (size_t)app->width * 4);
    app->have_desktop = true;
    app->video_frames++;
  }

  if (meta) {
    bool changed = false;
    if (!spa_meta_cursor_is_valid(meta)) {
      if (app->cursor.visible) {
        app->cursor.visible = false;
        app->cursor_hidden_events++;
        changed = true;
        fprintf(stderr, "CURSOR visibility=hidden\n");
      }
    } else {
      if (!app->cursor.visible || app->cursor.x != meta->position.x ||
          app->cursor.y != meta->position.y) {
        app->cursor_position_events++;
        changed = true;
      }
      app->cursor.visible = true;
      app->cursor.x = meta->position.x;
      app->cursor.y = meta->position.y;
      if (meta->position.x < 0 || meta->position.y < 0 ||
          meta->position.x >= (int32_t)app->width ||
          meta->position.y >= (int32_t)app->height)
        app->out_of_bounds_positions++;

      if (meta->bitmap_offset >= sizeof(*meta)) {
        struct spa_meta_bitmap *bitmap = SPA_PTROFF(meta, meta->bitmap_offset,
                                                     struct spa_meta_bitmap);
        app->cursor.hotspot_x = meta->hotspot.x;
        app->cursor.hotspot_y = meta->hotspot.y;
        if (spa_meta_bitmap_is_valid(bitmap) && bitmap->offset >= sizeof(*bitmap) &&
            bitmap->size.width <= CURSOR_MAX && bitmap->size.height <= CURSOR_MAX &&
            bitmap->stride >= (int32_t)(bitmap->size.width * 4) && bitmap->stride > 0) {
          const uint8_t *src = SPA_PTROFF(bitmap, bitmap->offset, uint8_t);
          size_t bytes = (size_t)bitmap->stride * bitmap->size.height;
          if (bytes <= sizeof(app->cursor.pixels)) {
            memcpy(app->cursor.pixels, src, bytes);
            app->cursor.width = bitmap->size.width;
            app->cursor.height = bitmap->size.height;
            app->cursor.stride = bitmap->stride;
            app->cursor.format = bitmap->format;
            app->cursor.have_bitmap = true;
            app->cursor.bitmap_hash = fnv1a(src, bytes);
            app->cursor_bitmap_events++;
            if (app->cursor.bitmap_hash != app->last_shape_hash) {
              app->last_shape_hash = app->cursor.bitmap_hash;
              app->distinct_shapes++;
              fprintf(stderr,
                      "CURSOR shape=%" PRIu64 " format=%u size=%ux%u stride=%d hotspot=%d,%d\n",
                      app->cursor.bitmap_hash, bitmap->format, bitmap->size.width,
                      bitmap->size.height, bitmap->stride, meta->hotspot.x,
                      meta->hotspot.y);
            }
            changed = true;
          }
        } else {
          app->cursor.have_bitmap = false;
          app->cursor.width = app->cursor.height = 0;
          changed = true;
          fprintf(stderr, "CURSOR bitmap=empty visibility=hidden-shape\n");
        }
      }
    }
    if (changed) {
      app->cursor_events++;
      app->cursor.generation++;
      app->cursor.received_ns = now;
      fprintf(stderr, "CURSOR event=%" PRIu64 " visible=%d position=%d,%d bitmap=%d\n",
              app->cursor_events, app->cursor.visible, app->cursor.x,
              app->cursor.y, app->cursor.have_bitmap);
    }
  }
  pthread_mutex_unlock(&app->lock);
  pw_stream_queue_buffer(app->stream, buffer);
}

static void on_state_changed(void *opaque, enum pw_stream_state old,
                             enum pw_stream_state state, const char *error) {
  struct app *app = opaque;
  fprintf(stderr, "STREAM old=%s state=%s error=%s\n",
          pw_stream_state_as_string(old), pw_stream_state_as_string(state),
          error ? error : "none");
  if (state == PW_STREAM_STATE_ERROR || state == PW_STREAM_STATE_UNCONNECTED)
    request_stop(app);
}

static void on_param_changed(void *opaque, uint32_t id, const struct spa_pod *param) {
  struct app *app = opaque;
  if (!param || id != SPA_PARAM_Format)
    return;

  struct spa_video_info_raw info = {0};
  if (spa_format_video_raw_parse(param, &info) < 0) {
    fprintf(stderr, "ERROR failed to parse negotiated raw video format\n");
    request_stop(app);
    return;
  }
  fprintf(stderr, "FORMAT format=%u width=%u height=%u rate=%u/%u\n",
          info.format, info.size.width, info.size.height,
          info.framerate.num, info.framerate.denom);
  if (info.format != SPA_VIDEO_FORMAT_BGRx || info.size.width != app->width ||
      info.size.height != app->height) {
    fprintf(stderr, "ERROR unexpected negotiated format/size\n");
    request_stop(app);
    return;
  }

  uint8_t pod_buffer[2048];
  struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(pod_buffer, sizeof(pod_buffer));
  const struct spa_pod *params[4];
  uint32_t n = 0;
  params[n++] = spa_pod_builder_add_object(
      &builder, SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
      SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(8, 2, 16),
      SPA_PARAM_BUFFERS_blocks, SPA_POD_Int(1),
      SPA_PARAM_BUFFERS_size, SPA_POD_Int(app->width * app->height * 4),
      SPA_PARAM_BUFFERS_stride, SPA_POD_Int(app->width * 4),
      SPA_PARAM_BUFFERS_dataType,
      SPA_POD_CHOICE_FLAGS_Int((1u << SPA_DATA_MemPtr) | (1u << SPA_DATA_MemFd)));
  params[n++] = spa_pod_builder_add_object(
      &builder, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
      SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Header),
      SPA_PARAM_META_size, SPA_POD_Int(sizeof(struct spa_meta_header)));
  params[n++] = spa_pod_builder_add_object(
      &builder, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
      SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Cursor),
      SPA_PARAM_META_size,
      SPA_POD_CHOICE_RANGE_Int(CURSOR_META_SIZE(CURSOR_MAX, CURSOR_MAX),
                               sizeof(struct spa_meta_cursor),
                               CURSOR_META_SIZE(CURSOR_MAX, CURSOR_MAX)));
  params[n++] = spa_pod_builder_add_object(
      &builder, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
      SPA_PARAM_META_type, SPA_POD_Id(SPA_META_VideoDamage),
      SPA_PARAM_META_size, SPA_POD_CHOICE_RANGE_Int(
          sizeof(struct spa_meta_region) * 16, sizeof(struct spa_meta_region),
          sizeof(struct spa_meta_region) * 16));
  pw_stream_update_params(app->stream, params, n);
}

static const struct pw_stream_events stream_events = {
  PW_VERSION_STREAM_EVENTS,
  .state_changed = on_state_changed,
  .param_changed = on_param_changed,
  .process = on_process,
};

static int start_preview(struct app *app) {
  GError *error = NULL;
  char pipeline[512];
  snprintf(pipeline, sizeof(pipeline),
           "appsrc name=preview_src is-live=true format=time block=false "
           "caps=video/x-raw,format=BGRx,width=%u,height=%u,framerate=%u/1 "
           "! queue max-size-buffers=2 leaky=downstream ! videoconvert "
           "! autovideosink sync=false",
           app->width, app->height, app->fps);
  app->preview_pipeline = gst_parse_launch(pipeline, &error);
  if (!app->preview_pipeline) {
    fprintf(stderr, "ERROR preview pipeline: %s\n", error ? error->message : "unknown");
    g_clear_error(&error);
    return -1;
  }
  GstElement *src = gst_bin_get_by_name(GST_BIN(app->preview_pipeline), "preview_src");
  if (!src) {
    fprintf(stderr, "ERROR preview appsrc missing\n");
    return -1;
  }
  app->preview_src = GST_APP_SRC(src);
  GstStateChangeReturn state = gst_element_set_state(app->preview_pipeline, GST_STATE_PLAYING);
  if (state == GST_STATE_CHANGE_FAILURE) {
    fprintf(stderr, "ERROR preview failed to enter PLAYING\n");
    return -1;
  }
  return 0;
}

int main(int argc, char **argv) {
  if (argc != 6) {
    fprintf(stderr, "usage: %s REMOTE_FD NODE_ID WIDTH HEIGHT FPS\n", argv[0]);
    return 2;
  }
  struct app app = {0};
  app.remote_fd = atoi(argv[1]);
  app.node_id = (uint32_t)strtoul(argv[2], NULL, 10);
  app.width = (uint32_t)strtoul(argv[3], NULL, 10);
  app.height = (uint32_t)strtoul(argv[4], NULL, 10);
  app.fps = (uint32_t)strtoul(argv[5], NULL, 10);
  if (app.remote_fd < 0 || !app.width || !app.height || !app.fps)
    return 2;

  pthread_mutex_init(&app.lock, NULL);
  app.desktop = calloc((size_t)app.width * app.height, 4);
  if (!app.desktop) {
    perror("calloc desktop");
    return 1;
  }
  app.started_ns = mono_ns();
  global_app = &app;
  signal(SIGINT, signal_handler);
  signal(SIGTERM, signal_handler);

  gst_init(&argc, &argv);
  pw_init(&argc, &argv);
  if (start_preview(&app) < 0)
    goto fail;

  app.loop = pw_main_loop_new(NULL);
  app.context = pw_context_new(pw_main_loop_get_loop(app.loop), NULL, 0);
  int owned_fd = fcntl(app.remote_fd, F_DUPFD_CLOEXEC, 3);
  app.core = pw_context_connect_fd(app.context, owned_fd, NULL, 0);
  if (!app.core) {
    fprintf(stderr, "ERROR pw_context_connect_fd: %s\n", strerror(errno));
    goto fail;
  }
  struct pw_properties *props = pw_properties_new(
      PW_KEY_MEDIA_TYPE, "Video", PW_KEY_MEDIA_CATEGORY, "Capture",
      PW_KEY_MEDIA_ROLE, "Screen", PW_KEY_TARGET_OBJECT, argv[2], NULL);
  app.stream = pw_stream_new(app.core, "USBdisplay Phase42C cursor probe", props);
  if (!app.stream) {
    fprintf(stderr, "ERROR pw_stream_new\n");
    goto fail;
  }
  pw_stream_add_listener(app.stream, &app.stream_listener, &stream_events, &app);

  uint8_t format_buffer[1024];
  struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(format_buffer, sizeof(format_buffer));
  struct spa_rectangle size = SPA_RECTANGLE(app.width, app.height);
  struct spa_fraction variable_rate = SPA_FRACTION(0, 1);
  struct spa_fraction rate = SPA_FRACTION(app.fps, 1);
  const struct spa_pod *params[1];
  params[0] = spa_pod_builder_add_object(
      &builder, SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
      SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
      SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
      SPA_FORMAT_VIDEO_format, SPA_POD_Id(SPA_VIDEO_FORMAT_BGRx),
      SPA_FORMAT_VIDEO_size, SPA_POD_Rectangle(&size),
      SPA_FORMAT_VIDEO_framerate, SPA_POD_Fraction(&variable_rate),
      SPA_FORMAT_VIDEO_maxFramerate, SPA_POD_Fraction(&rate));

  int result = pw_stream_connect(
      app.stream, PW_DIRECTION_INPUT, app.node_id,
      PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS, params, 1);
  if (result < 0) {
    fprintf(stderr, "ERROR pw_stream_connect: %s\n", spa_strerror(result));
    goto fail;
  }
  if (pthread_create(&app.output_thread, NULL, output_main, &app) != 0) {
    fprintf(stderr, "ERROR pthread_create\n");
    goto fail;
  }
  app.output_started = true;
  fprintf(stderr, "READY node=%u size=%ux%u fps=%u cursor_mode=metadata preview=host-only\n",
          app.node_id, app.width, app.height, app.fps);
  pw_main_loop_run(app.loop);

fail:
  app.stopping = 1;
  if (app.output_started)
    pthread_join(app.output_thread, NULL);
  print_metrics(&app, "final");
  if (app.preview_pipeline) {
    gst_element_set_state(app.preview_pipeline, GST_STATE_NULL);
    gst_object_unref(app.preview_pipeline);
  }
  if (app.preview_src)
    gst_object_unref(app.preview_src);
  if (app.stream)
    pw_stream_destroy(app.stream);
  if (app.core)
    pw_core_disconnect(app.core);
  if (app.context)
    pw_context_destroy(app.context);
  if (app.loop)
    pw_main_loop_destroy(app.loop);
  pw_deinit();
  free(app.desktop);
  pthread_mutex_destroy(&app.lock);
  return app.source_buffers > 0 ? 0 : 1;
}
