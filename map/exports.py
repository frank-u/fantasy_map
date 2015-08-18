import math
import numpy as np
import os
from osgeo import gdal
from osgeo import osr

from django.conf import settings
from django.contrib.gis.geos import Polygon, MultiPolygon, MultiLineString, LineString
from noise import snoise2
from scipy.ndimage.filters import median_filter
from shapely.geometry import Polygon as Poly


class ModelExporter:

    def __init__(self, model, river_model, max_lat, max_lng):
        self.model = model
        self.river_model = river_model
        self.max_lat = max_lat
        self.max_lng = max_lng

    def export(self, map_obj):
        print('Export data to DB')
        # Export biomes
        self.model.objects.all().delete()
        new_objects = []
        print('Save biomes')

        for center in map_obj.centers:
            obj = self.model()
            center.model = obj
            obj.biome = center.biome
            obj.water = center.water
            obj.coast = center.coast
            obj.border = center.border
            obj.elevation = center.elevation
            obj.moisture = center.moisture
            obj.lng, obj.lat = self.point_to_lnglat(center.point)
            obj.river = any(edge.river for edge in center.borders)

            coords = []
            for corner in center.corners:
                coords.append(self.point_to_lnglat(corner.point))
            # Sort coordinates. Should be sorted already, but lets check once more.
            coords.sort(key=lambda p: math.atan2(p[1] - obj.lat, p[0] - obj.lng))
            coords.append(coords[0])

            obj.geom = MultiPolygon([Polygon(coords)])
            obj.full_clean()
            obj.save()
            new_objects.append(obj)

        # FIXME: Use bulk_create and change neighbors saving
        # self.model.objects.bulk_create(new_objects)

        # save neighbors
        print('Save biomes neighbors')
        checked = []
        for center in map_obj.centers:
            checked.append(center)
            for neighbour in center.neighbors:
                if neighbour not in checked:
                    center.model.neighbors.add(neighbour.model)

        # Export rivers
        print('Save rivers')
        self.river_model.objects.all().delete()
        new_objects = []

        for edge in map_obj.edges:
            if edge.river:
                obj = self.river_model()
                obj.width = edge.river
                p1 = self.point_to_lnglat(edge.corners[0].point)
                p2 = self.point_to_lnglat(edge.corners[1].point)
                obj.geom = MultiLineString(LineString(p1, p2))
                obj.full_clean()
                new_objects.append(obj)

        self.river_model.objects.bulk_create(new_objects)

    def point_to_lnglat(self, point):
        return (
            self.max_lng * point[0] - self.max_lng / 2,
            self.max_lat * point[1] - self.max_lat / 2
        )


class GeoTiffExporter(object):

    def __init__(self, max_lat, max_lng, width=1000, hill_noise=True):
        self.max_lat = max_lat
        self.max_lng = max_lng
        self.dst_filename = os.path.join(settings.BASE_DIR, 'map.tif')
        self.top_left_point = (-(max_lng / 2), max_lat / 2)
        self.bot_right_point = (max_lng / 2, -(max_lat / 2))
        self.max_height = 500  # elevation will be scaled to this value
        self.width = width
        self.hill_noise = hill_noise

    # @profile  # 5624515 function calls in 9.509 seconds
    def export(self, map_obj):
        # http://www.gdal.org/gdal_tutorial.html
        # http://blambi.blogspot.com/2010/05/making-geo-referenced-images-in-python.html
        in_srs = self.get_in_projection()
        out_srs = self.get_out_projection()
        coord_transform = osr.CoordinateTransformation(in_srs, out_srs)

        top_left_lng_m, top_left_lat_m, _ = coord_transform.TransformPoint(*self.top_left_point)
        bot_right_lng_m, bot_right_lat_m, _ = coord_transform.TransformPoint(*self.bot_right_point)

        # image size
        x_pixels = self.width
        pixel_size = abs(top_left_lng_m - bot_right_lng_m) / x_pixels
        y_pixels = int(abs(bot_right_lat_m - top_left_lat_m) / pixel_size) + 1
        x_pixels += 1

        # pixel/coords transform and inverse transform
        geo = [top_left_lng_m, pixel_size, 0, top_left_lat_m, 0, -pixel_size]
        inv_geo = gdal.InvGeoTransform(geo)[1]
        image_data = self.get_image_data(map_obj, (y_pixels, x_pixels), inv_geo, coord_transform)

        image_data = median_filter(image_data, (6, 6))
        # image_data = gaussian_filter(image_data, sigma=1)
        if self.hill_noise:
            self.add_noise(image_data, map_obj.seed)

        image_data *= self.max_height
        image_data = self.add_hillshade(image_data, 225, 45)

        # create image
        dataset = gdal.GetDriverByName('GTiff').Create(
            self.dst_filename,
            x_pixels,
            y_pixels,
            1,  # bands count
            gdal.GDT_Byte)

        dataset.SetGeoTransform(geo)
        dataset.SetProjection(out_srs.ExportToWkt())
        dataset.GetRasterBand(1).WriteArray(image_data)
        dataset.FlushCache()

    def get_image_data(self, map_obj, size, inv_geo, coord_transform):
        cache_file_name = 'height_map_cache/%s_%s_%s.npy' % (map_obj.seed, len(map_obj.points), self.width)
        cache_file_path = os.path.join(settings.BASE_DIR, cache_file_name)

        try:
            return np.load(cache_file_path)
        except IOError:
            pass

        raster = np.zeros(size, dtype=np.float32)

        step = 0.5 / size[0]
        count = len(map_obj.centers)
        completed = 0
        for center in map_obj.centers:
            completed += 1
            if completed % 100 == 0:
                print('%s of %s' % (completed, count))

            if center.water:
                continue

            v1 = np.array([center.point[0], center.point[1], center.elevation])

            for edge in center.borders:
                c1 = edge.corners[0]
                c2 = edge.corners[1]
                cp1 = c1.point
                cp2 = c2.point

                # get the equation of a plane from three points
                v2 = np.array([cp1[0], cp1[1], c1.elevation])
                v3 = np.array([cp2[0], cp2[1], c2.elevation])
                normal = np.cross(v2 - v1, v3 - v1)
                a, b, c = normal
                d = np.dot(normal, v3)

                # calculate elevation for all points in polygon
                poly = Poly([center.point, cp1, cp2])
                minx, miny, maxx, maxy = poly.bounds

                # TODO: requires some optimization, too many checks here
                for x in np.arange(minx, maxx, step):
                    for y in np.arange(miny, maxy, step):
                        if in_triange((x, y), v1, cp1, cp2):
                            # calculate elevation and convert to pixel value
                            z = (a * x + b * y - d) / -c
                            # get pixel coordinates from our coordinates(0-1)
                            img_x, img_y = self.point_to_pixel((x, y), inv_geo, coord_transform)
                            raster[img_y][img_x] = z

        np.save(cache_file_path, raster)
        return raster

    def get_in_projection(self):
        """
        We save our polygons in this projection.
        """
        proj = osr.SpatialReference()
        proj.ImportFromEPSG(4326)
        return proj

    def get_out_projection(self):
        """
        Output projection is projection of our map tiles.
        """
        proj = osr.SpatialReference()
        proj.ImportFromEPSG(3857)
        return proj

    def get_pixel(self, lng, lat, inv_geo, transform):
        """
        Return pixel coordinates from lng/lat
        """
        gx, gy, _ = transform.TransformPoint(lng, lat)
        gx, gy = gdal.ApplyGeoTransform(inv_geo, gx, gy)
        return int(gx), int(gy)

    def point_to_lnglat(self, point):
        """
        Convert point in our coordinates(0-1) to lng/lat
        """
        return (
            self.max_lng * point[0] - self.max_lng / 2,
            self.max_lat * point[1] - self.max_lat / 2
        )

    def point_to_pixel(self, point, inv_geo, transform):
        """
        Convert point in our coordinates(0-1) to pixel coordinates
        """
        lng, lat = self.point_to_lnglat(point)
        return self.get_pixel(lng, lat, inv_geo, transform)

    def add_hillshade(self, image_data, azimuth, angle_altitude):
        """
        From here http://geoexamples.blogspot.com/2014/03/shaded-relief-images-using-gdal-python.html
        """
        x, y = np.gradient(image_data)
        slope = np.pi / 2. - np.arctan(np.sqrt(x * x + y * y))
        aspect = np.arctan2(-x, y)
        azimuthrad = azimuth * np.pi / 180.
        altituderad = angle_altitude*np.pi / 180.

        shaded = np.sin(altituderad) * np.sin(slope) + np.cos(altituderad) * np.cos(slope) \
            * np.cos(azimuthrad - aspect)
        return 255 * (shaded + 1) / 2

    def add_noise(self, image_data, seed):
        for y in range(image_data.shape[0]):
            for x in range(image_data.shape[1]):
                # large scale gives more frequent noise
                if image_data[y][x] > 0:
                    scale = 0.03
                    level = 0.004 + 0.004 * image_data[y][x]

                    noise = snoise2(x * scale, y * scale, octaves=2, base=seed) * level
                    image_data[y][x] = image_data[y][x] + noise
                    if image_data[y][x] < 0:
                        image_data[y][x] = 0


def in_triange(pt, v1, v2, v3):
    b1 = ((pt[0] - v2[0]) * (v1[1] - v2[1]) - (v1[0] - v2[0]) * (pt[1] - v2[1])) <= 0
    b2 = ((pt[0] - v3[0]) * (v2[1] - v3[1]) - (v2[0] - v3[0]) * (pt[1] - v3[1])) <= 0
    b3 = ((pt[0] - v1[0]) * (v3[1] - v1[1]) - (v3[0] - v1[0]) * (pt[1] - v1[1])) <= 0
    return (b1 == b2) and (b2 == b3)
