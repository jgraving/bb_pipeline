from datetime import timedelta
import hashlib
from itertools import chain
import uuid
import os
import subprocess as sp
from bb_binary import DataSource, FrameContainer, \
    parse_image_fname, parse_video_fname, get_timezone
from pipeline.objects import PipelineResult
import numpy as np


class VideoReader:
    def __init__(self, video_path,
                 ffmpeg_stderr_fd=None,
                 format='guess_on_ext',
                 ffmpeg_bin='ffmpeg',
                 ffprobe_bin='ffprobe'):
        if format == 'guess_on_ext':
            format = self.guess_format_on_extension(video_path)

        vidread_command = [
            ffmpeg_bin,
            '-i', video_path,
            '-f', 'image2pipe',
            '-pix_fmt', 'gray',
            '-vsync', '0',
            '-vcodec', 'rawvideo', '-'
        ]

        if format is not None:
            vidread_command.insert(1, '-vcodec')
            vidread_command.insert(2, format)

        resolution_command = [
            ffprobe_bin,
            '-v', 'error',
            '-of', 'flat=s=_',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=height,width',
            video_path
        ]

        pipe = sp.Popen(resolution_command, stdout=sp.PIPE, stderr=sp.PIPE)
        infos = pipe.stdout.readlines()
        self.w, self.h = [int(s.decode('utf-8').strip().split('=')[1]) for s in infos]

        self.video_pipe = sp.Popen(vidread_command,
                                   stdout=sp.PIPE,
                                   stderr=ffmpeg_stderr_fd)
        self.frames = 0

    @staticmethod
    def guess_format_on_extension(video_path):
        _, ext = os.path.splitext(video_path)
        if ext == '.mkv':
            format = None
        elif ext == '.avi':
            format = 'hevc'
        else:
            raise Exception("Unknown extension {}.".format(ext))
        return format

    def __iter__(self):
        return self

    def __next__(self):
        return self.next()

    def next(self):
        raw_image = self.video_pipe.stdout.read(self.h * self.w * 1)

        if len(raw_image) != self.h * self.w * 1:
            assert(len(raw_image) == 0)
            raise StopIteration()

        self.frames += 1

        image = np.fromstring(raw_image, dtype='uint8')
        image = image.reshape((self.h, self.w))
        self.video_pipe.stdout.flush()
        return image


def raw_frames_generator(path_video, format='guess_on_ext', stderr_fd=None):
    "yields the frames of the video."
    return VideoReader(path_video, stderr_fd, format=format)


def video_generator(path_video, ts_format='2016', path_filelists=None,
                    log_callback=None, stderr_fd=sp.DEVNULL):
    timestamps = get_timestamps(path_video, ts_format, path_filelists)
    fname_video = os.path.basename(path_video)
    data_source = DataSource.new_message(filename=fname_video)
    for i, frame in enumerate(VideoReader(path_video, stderr_fd)):
        if log_callback is not None:
            log_callback(i)
        img = frame
        yield data_source, img, timestamps[i]


class Sink:
    def add_frame(self, data_source, frame):
        raise NotImplemented()

    def finish(self):
        raise NotImplemented()


def unique_id():
    hasher = hashlib.sha1()
    hasher.update(uuid.uuid4().bytes)
    hash = int.from_bytes(hasher.digest(), byteorder='big')
    # strip to 64 bits
    hash = hash >> (hash.bit_length() - 64)
    return hash


class BBBinaryRepoSink(Sink):
    def __init__(self, repo, camId):
        self.repo = repo
        self.frames = []
        self.data_sources_fname = []
        self.data_sources = []
        self.camId = camId

    def add_frame(self, data_source, results, timestamp):
        detections = results[PipelineResult]
        fname = data_source.filename
        if fname not in self.data_sources_fname:
            self.data_sources.append(data_source)
            self.data_sources_fname.append(fname)
        data_source_idx = self.data_sources_fname.index(fname)
        self.frames.append((data_source_idx, detections, timestamp))

    def _get_container(self):
        TIMESTAMP_IDX = 2
        self.frames.sort(key=lambda x: x[TIMESTAMP_IDX])
        start_ts = self.frames[0][TIMESTAMP_IDX]
        end_ts = self.frames[-1][TIMESTAMP_IDX]
        fc = FrameContainer.new_message(fromTimestamp=start_ts,
                                        toTimestamp=end_ts,
                                        camId=self.camId,
                                        id=unique_id())
        dataSources = fc.init('dataSources', len(self.data_sources))
        for i, dsource in enumerate(self.data_sources):
            dataSources[i] = dsource
            dataSources[i].idx = int(i)

        frames = fc.init('frames', len(self.frames))
        for i, (data_source_idx, detection, timestamp) in enumerate(self.frames):
            frame = frames[i]
            frame.id = unique_id()
            frame.dataSourceIdx = data_source_idx
            frame.frameIdx = int(i)
            frame.timestamp = timestamp
            detections_builder = frame.detectionsUnion.init(
                'detectionsDP', len(detection.positions))
            for j, db in enumerate(detections_builder):
                db.idx = j
                db.xpos = int(detection.positions[j, 1])
                db.ypos = int(detection.positions[j, 0])
                db.xposHive = int(detection.hive_positions[j, 1])
                db.yposHive = int(detection.hive_positions[j, 0])
                db.zRotation = float(detection.orientations[j, 0])
                db.yRotation = float(detection.orientations[j, 1])
                db.xRotation = float(detection.orientations[j, 2])
                db.localizerSaliency = float(detection.saliencies[j, 0])
                db.radius = float(detection.radii[j])
                descriptor = db.init('descriptor', len(detection.descriptors[j]))
                for k, part in enumerate(detection.descriptors[j]):
                    descriptor[k] = int(part)
                decodedId = db.init('decodedId', len(detection.ids[j]))
                for k, bit in enumerate(detection.ids[j]):
                    decodedId[k] = int(round(255*bit))

        return fc

    def finish(self):
        self.repo.add(self._get_container())


def get_seperate_timestamps(path_video, ts_format, path_filelists):
    def get_flist_name(dt_utc):
        fmt = '%Y%m%d'
        dt = dt_utc.astimezone(get_timezone())
        if ts_format == '2014':
            return dt.strftime(fmt) + '.txt'
        elif ts_format == '2015':
            return os.path.join(dt.strftime(fmt), 'images.txt')
        else:
            assert(False)

    def find_file(name, path):
        for root, dirs, files in os.walk(path):
            if name in [os.path.join(os.path.basename(root), f) for f in files]:
                return os.path.join(path, name)
        return None

    def next_day(dt):
        return dt + timedelta(days=1)

    fname_video = os.path.basename(path_video)
    cam, from_dt, to_dt = parse_video_fname(fname_video)
    # due to a bug in the data aquistion software we have to check in the
    # filename list of the next day as well
    timestamps = [from_dt, to_dt, next_day(from_dt), next_day(to_dt)]
    txt_files = set([get_flist_name(dt) for dt in timestamps])
    txt_paths = [find_file(f, path_filelists) for f in txt_files]
    txt_paths = [path for path in txt_paths if path is not None]
    assert len(txt_paths) > 0

    image_fnames = list(chain.from_iterable([open(path, 'r').readlines() for path in txt_paths]))
    first_fname = fname_video.split('_TO_')[0] + '.jpeg\n'
    second_fname = fname_video.split('_TO_')[1].split('.mkv')[0] + '.jpeg\n'
    image_fnames.sort()

    fnames = image_fnames[image_fnames.index(first_fname):image_fnames.index(second_fname) + 1]
    return [parse_image_fname(fn, format='beesbook')[1].timestamp() for fn in fnames]


def get_timestamps(path_video, ts_format, path_filelists):
    # In season 2014 and 2015, the timestamps for each video were stored in a seperate location
    # from the videos themselves. From 2016 onwards a text file with the timestamps of all frames
    # is stored next to the videos with the same filename (apart from the extension).
    if ts_format == '2016':
        assert path_filelists is None
        txt_path = path_video.replace('mkv', 'txt')
        fnames = open(txt_path, 'r').readlines()
        return [parse_image_fname(fn, format='iso')[1].timestamp() for fn in fnames]
    elif ts_format in ('2014', '2015'):
        assert path_filelists is not None
        return get_seperate_timestamps(path_video, ts_format, path_filelists)
    else:
        assert False, 'Unknown timestamp format'
