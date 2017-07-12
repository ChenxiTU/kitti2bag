
#!env python
# -*- coding: utf-8 -*-

import sys

try:
    import pykitti
except ImportError as e:
    print('Could not load module \'pykitti\'. Please run `pip install pykitti`')
    sys.exit(1)

import tf
import os
import cv2
import rospy
import rosbag
import progressbar
from tf2_msgs.msg import TFMessage
from datetime import datetime
from std_msgs.msg import Header
from sensor_msgs.msg import CameraInfo, Imu, PointField, NavSatFix
import sensor_msgs.point_cloud2 as pcl2
from geometry_msgs.msg import TransformStamped, TwistStamped, Transform
from cv_bridge import CvBridge
import numpy as np


def save_imu_data(bag, kitti, imu_frame_id, topic):
    print("Exporting IMU")
    for timestamp, oxts in zip(kitti.timestamps, kitti.oxts):
        q = tf.transformations.quaternion_from_euler(oxts.packet.roll, oxts.packet.pitch, oxts.packet.yaw)
        imu = Imu()
        imu.header.frame_id = imu_frame_id
        imu.header.stamp = rospy.Time.from_sec(float(timestamp.strftime("%s.%f")))
        imu.orientation.x = q[0]
        imu.orientation.y = q[1]
        imu.orientation.z = q[2]
        imu.orientation.w = q[3]
        imu.linear_acceleration.x = oxts.packet.af
        imu.linear_acceleration.y = oxts.packet.al
        imu.linear_acceleration.z = oxts.packet.au
        imu.angular_velocity.x = oxts.packet.wf
        imu.angular_velocity.y = oxts.packet.wl
        imu.angular_velocity.z = oxts.packet.wu
        bag.write(topic, imu, t=imu.header.stamp)


def save_dynamic_tf(bag, kitti):
    print("Exporting time dependent transformations")
    for timestamp, oxts in zip(kitti.timestamps, kitti.oxts):
        tf_oxts_msg = TFMessage()
        tf_oxts_transform = TransformStamped()
        tf_oxts_transform.header.stamp = rospy.Time.from_sec(float(timestamp.strftime("%s.%f")))
        tf_oxts_transform.header.frame_id = 'world'
        tf_oxts_transform.child_frame_id = 'base_link'

        transform = (oxts.T_w_imu)
        t = transform[0:3, 3]
        q = tf.transformations.quaternion_from_matrix(transform)
        oxts_tf = Transform()

        oxts_tf.translation.x = t[0]
        oxts_tf.translation.y = t[1]
        oxts_tf.translation.z = t[2]

        oxts_tf.rotation.x = q[0]
        oxts_tf.rotation.y = q[1]
        oxts_tf.rotation.z = q[2]
        oxts_tf.rotation.w = q[3]

        tf_oxts_transform.transform = oxts_tf
        tf_oxts_msg.transforms.append(tf_oxts_transform)

        bag.write('/tf', tf_oxts_msg, tf_oxts_msg.transforms[0].header.stamp)


def save_camera_data(bag, kitti, util, bridge, camera, camera_frame_id, topic):
    print("Exporting camera {}".format(camera))
    camera_pad = '{0:02d}'.format(camera)
    image_path = os.path.join(kitti.data_path, 'image_{}'.format(camera_pad))
    img_data_dir = os.path.join(image_path, 'data')
    img_filenames = sorted(os.listdir(img_data_dir))
    with open(os.path.join(image_path, 'timestamps.txt')) as f:
        img_datetimes = map(lambda x: datetime.strptime(x[:-4], '%Y-%m-%d %H:%M:%S.%f'), f.readlines())

    calib = CameraInfo()
    calib.header.frame_id = camera_frame_id
    calib.width, calib.height = tuple(util['S_rect_{}'.format(camera_pad)].tolist())
    calib.distortion_model = 'plumb_bob'
    calib.K = util['K_{}'.format(camera_pad)]
    calib.R = util['R_rect_{}'.format(camera_pad)]
    calib.D = util['D_{}'.format(camera_pad)]
    calib.P = util['P_rect_{}'.format(camera_pad)]

    iterable = zip(img_datetimes, img_filenames)
    bar = progressbar.ProgressBar()
    for dt, filename in bar(iterable):
        img_filename = os.path.join(img_data_dir, filename)
        cv_image = cv2.imread(img_filename)
        if camera in (0, 1):
            cv_image = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
        encoding = "mono8" if camera in (0, 1) else "bgr8"
        image_message = bridge.cv2_to_imgmsg(cv_image, encoding=encoding)
        image_message.header.frame_id = camera_frame_id
        image_message.header.stamp = rospy.Time.from_sec(float(datetime.strftime(dt, "%s.%f")))
        calib.header.stamp = image_message.header.stamp
        bag.write(topic + '/image_raw', image_message, t=image_message.header.stamp)
        bag.write(topic + '/camera_info', calib, t=calib.header.stamp)


def save_velo_data(bag, kitti, velo_frame_id, topic):
    print("Exporting velodyne data")
    velo_path = os.path.join(kitti.data_path, 'velodyne_points')
    velo_data_dir = os.path.join(velo_path, 'data')
    velo_filenames = sorted(os.listdir(velo_data_dir))
    with open(os.path.join(velo_path, 'timestamps.txt')) as f:
        lines = f.readlines()
        velo_datetimes = []
        for line in lines:
            if len(line) == 1:
                continue
            dt = datetime.strptime(line[:-4], '%Y-%m-%d %H:%M:%S.%f')
            velo_datetimes.append(dt)

    iterable = zip(velo_datetimes, velo_filenames)
    bar = progressbar.ProgressBar()
    for dt, filename in bar(iterable):
        if dt is None:
            continue

        velo_filename = os.path.join(velo_data_dir, filename)

        # read binary data
        scan = (np.fromfile(velo_filename, dtype=np.float32)).reshape(-1, 4)

        # create header
        header = Header()
        header.frame_id = velo_frame_id
        header.stamp = rospy.Time.from_sec(float(datetime.strftime(dt, "%s.%f")))

        # fill pcl msg
        fields = [PointField('x', 0, PointField.FLOAT32, 1),
                  PointField('y', 4, PointField.FLOAT32, 1),
                  PointField('z', 8, PointField.FLOAT32, 1),
                  PointField('i', 12, PointField.FLOAT32, 1)]
        pcl_msg = pcl2.create_cloud(header, fields, scan)

        bag.write(topic + '/pointcloud', pcl_msg, t=pcl_msg.header.stamp)


def get_static_transform(from_frame_id, to_frame_id, transform):
    t = transform[0:3, 3]
    q = tf.transformations.quaternion_from_matrix(transform)
    tf_msg = TransformStamped()
    tf_msg.header.frame_id = from_frame_id
    tf_msg.child_frame_id = to_frame_id
    tf_msg.transform.translation.x = float(t[0])
    tf_msg.transform.translation.y = float(t[1])
    tf_msg.transform.translation.z = float(t[2])
    tf_msg.transform.rotation.x = float(q[0])
    tf_msg.transform.rotation.y = float(q[1])
    tf_msg.transform.rotation.z = float(q[2])
    tf_msg.transform.rotation.w = float(q[3])
    return tf_msg


def inv(transform):
    "Invert rigid body transformation matrix"
    R = transform[0:3, 0:3]
    t = transform[0:3, 3]
    t_inv = -1 * R.T.dot(t)
    transform_inv = np.eye(4)
    transform_inv[0:3, 0:3] = R.T
    transform_inv[0:3, 3] = t_inv
    return transform_inv


def save_static_transforms(bag, transforms, timestamps):
    print("Exporting static transformations")
    tfm = TFMessage()
    for transform in transforms:
        t = get_static_transform(from_frame_id=transform[0], to_frame_id=transform[1], transform=transform[2])
        tfm.transforms.append(t)
    for timestamp in timestamps:
        time = rospy.Time.from_sec(float(timestamp.strftime("%s.%f")))
        for i in range(len(tfm.transforms)):
            tfm.transforms[i].header.stamp = time
        bag.write('/tf_static', tfm, t=time)


def save_gps_fix_data(bag, kitti, gps_frame_id, topic):
    for timestamp, oxts in zip(kitti.timestamps, kitti.oxts):
        navsatfix_msg = NavSatFix()
        navsatfix_msg.header.frame_id = gps_frame_id
        navsatfix_msg.header.stamp = rospy.Time.from_sec(float(timestamp.strftime("%s.%f")))
        navsatfix_msg.latitude = oxts.packet.lat
        navsatfix_msg.longitude = oxts.packet.lon
        navsatfix_msg.altitude = oxts.packet.alt
        navsatfix_msg.status.service = 1
        bag.write(topic, navsatfix_msg, t=navsatfix_msg.header.stamp)


def save_gps_vel_data(bag, kitti, gps_frame_id, topic):
    for timestamp, oxts in zip(kitti.timestamps, kitti.oxts):
        twist_msg = TwistStamped()
        twist_msg.header.frame_id = gps_frame_id
        twist_msg.header.stamp = rospy.Time.from_sec(float(timestamp.strftime("%s.%f")))
        twist_msg.twist.linear.x = oxts.packet.vf
        twist_msg.twist.linear.y = oxts.packet.vl
        twist_msg.twist.linear.z = oxts.packet.vu
        twist_msg.twist.angular.x = oxts.packet.wf
        twist_msg.twist.angular.y = oxts.packet.wl
        twist_msg.twist.angular.z = oxts.packet.wu
        bag.write(topic, twist_msg, t=twist_msg.header.stamp)


def main():
    if len(sys.argv) not in (3, 4):
        print("Usage: kitti2bag base_dir date drive")
        print("   or: kitti2bag date drive (base_dir is then current dir)")
        return
    basedir = sys.argv[1] if len(sys.argv) == 4 else os.getcwd()
    date, drive = tuple(sys.argv[-2:])

    bridge = CvBridge()
    compression = rosbag.Compression.NONE
    # compression = rosbag.Compression.BZ2
    # compression = rosbag.Compression.LZ4
    bag = rosbag.Bag("kitti_{}_drive_{}_sync.bag".format(date, drive), 'w', compression=compression)
    kitti = pykitti.raw(basedir, date, drive)
    if not os.path.exists(kitti.data_path):
        print('Path {} does not exists. Exiting.'.format(kitti.data_path))
        sys.exit(1)

    kitti._load_calib()
    #kitti.oxts()
    kitti._load_timestamps()
    # kitti.load_velo()

    if len(kitti.timestamps) == 0:
        print('Dataset is empty? Exiting.')
        sys.exit(1)

    try:
        # IMU
        imu_frame_id = 'imu_link'
        imu_topic = '/kitti/oxts/imu'
        gps_fix_topic = '/kitti/oxts/gps/fix'
        gps_vel_topic = '/kitti/oxts/gps/vel'
        velo_frame_id = 'velo_link'
        velo_topic = '/kitti/velo'

        # CAMERAS
        cameras = [
            (0, 'camera_gray_left', '/kitti/camera_gray_left'),
            (1, 'camera_gray_right', '/kitti/camera_gray_right'),
            (2, 'camera_color_left', '/kitti/camera_color_left'),
            (3, 'camera_color_right', '/kitti/camera_color_right')
        ]

        T_base_link_to_imu = np.eye(4, 4)
        T_base_link_to_imu[0:3, 3] = [-2.71/2.0-0.05, 0.32, 0.93]

        # tf_static
        transforms = [
            ('base_link', imu_frame_id, T_base_link_to_imu),
            (imu_frame_id, velo_frame_id, inv(kitti.calib.T_velo_imu)),
            (imu_frame_id, cameras[0][1], inv(kitti.calib.T_cam0_imu)),
            (imu_frame_id, cameras[1][1], inv(kitti.calib.T_cam1_imu)),
            (imu_frame_id, cameras[2][1], inv(kitti.calib.T_cam2_imu)),
            (imu_frame_id, cameras[3][1], inv(kitti.calib.T_cam3_imu))
        ]

        util = pykitti.utils.read_calib_file(os.path.join(kitti.calib_path, 'calib_cam_to_cam.txt'))

        # Export
        save_static_transforms(bag, transforms, kitti.timestamps)
        save_dynamic_tf(bag, kitti)
        save_imu_data(bag, kitti, imu_frame_id, imu_topic)
        save_gps_fix_data(bag, kitti, imu_frame_id, gps_fix_topic)
        save_gps_vel_data(bag, kitti, imu_frame_id, gps_vel_topic)
        for camera in cameras:
            save_camera_data(bag, kitti, util, bridge, camera=camera[0], camera_frame_id=camera[1], topic=camera[2])
        save_velo_data(bag, kitti, velo_frame_id, velo_topic)


    finally:
        print("## OVERVIEW ##")
        print(bag)
        bag.close()


if __name__ == '__main__':
     main()
