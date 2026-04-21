#!/usr/bin/env python
# -*- coding: utf-8 -*-
from pymongo import MongoClient
from googlemaps import GoogleMapsScraper
from datetime import datetime, timedelta
import argparse
import logging
import sys
import os
import re

DB_URL = 'mongodb://localhost:27017/'
DB_NAME = 'googlemaps'
COLLECTION_NAME = 'review'

class Monitor:

    def __init__(self, url_file, from_date, mongourl=DB_URL):

        # cargar archivo de urls
        with open(url_file, 'r') as furl:
            self.urls = [u.strip() for u in furl if u.strip()]

        # definir conexión a MongoDB
        self.client = MongoClient(mongourl)

        # fecha mínima de reseña a scrapear
        self.min_date_review = datetime.strptime(from_date, '%Y-%m-%d')

        # logging
        self.logger = self.__get_logger()

    def scrape_gm_reviews(self):
        # establecer conexión a la base de datos
        collection = self.client[DB_NAME][COLLECTION_NAME]

        # inicializar scraper y agregar reseñas incrementalmente
        with GoogleMapsScraper() as scraper:
            for url in self.urls:
                try:
                    # ordenar por más recientes (índice 1)
                    error = scraper.sort_by(url, 1)
                    
                    if error == 0:
                        stop = False
                        offset = 0
                        n_new_reviews = 0
                        
                        while not stop:
                            rlist = scraper.get_reviews(offset)
                            
                            # si no hay más reseñas, salir
                            if len(rlist) == 0:
                                break
                            
                            for r in rlist:
                                # verificar si debemos detenernos
                                stop = self.__stop(r, collection)
                                
                                if not stop:
                                    collection.insert_one(r)
                                    n_new_reviews += 1
                                    self.logger.info('Nueva reseña agregada: {}'.format(r['id_review']))
                                else:
                                    break
                            
                            offset += len(rlist)

                        # registrar total de nuevas reseñas
                        self.logger.info('{} : {} nuevas reseñas agregadas'.format(url, n_new_reviews))
                    else:
                        self.logger.warning('Error al ordenar reseñas para {}'.format(url))

                except Exception as e:
                    exc_type, exc_obj, exc_tb = sys.exc_info()
                    fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                    self.logger.error('{}: {}, {}, {}'.format(url, exc_type, fname, exc_tb.tb_lineno))


    def __stop(self, r, collection):
        """
        Determina si debemos detenernos:
        - Si la reseña ya existe en la base de datos
        - Si la fecha de la reseña es anterior a min_date_review
        """
        # verificar si la reseña ya existe
        is_old_review = collection.find_one({'id_review': r['id_review']})
        
        # verificar si la fecha de la reseña es válida
        review_date = r.get('review_date')
        
        if is_old_review is not None:
            self.logger.info('Reseña duplicada encontrada: {}. Deteniendo...'.format(r['id_review']))
            return True
        
        if review_date and review_date < self.min_date_review:
            self.logger.info('Reseña anterior a la fecha mínima: {}. Deteniendo...'.format(review_date))
            return True
        
        return False

    def __get_logger(self):
        # crear logger
        logger = logging.getLogger('monitor')
        logger.setLevel(logging.DEBUG)
        
        # crear manejador de archivo y establecer nivel a debug
        fh = logging.FileHandler('monitor.log')
        fh.setLevel(logging.DEBUG)
        
        # crear manejador de consola para ver logs en tiempo real
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        
        # crear formateador
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        
        # agregar formateador a los manejadores
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)
        
        # agregar manejadores al logger
        logger.addHandler(fh)
        logger.addHandler(ch)

        return logger


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Monitoreo de reseñas de Google Maps')
    parser.add_argument('--i', type=str, default='urls.txt', help='Archivo de URLs objetivo')
    parser.add_argument('--from-date', type=str, required=True, help='Fecha de inicio en formato: AAAA-MM-DD')
    parser.add_argument('--db-url', type=str, default=DB_URL, help='URL de conexión a MongoDB')

    args = parser.parse_args()

    # validar formato de fecha
    try:
        datetime.strptime(args.from_date, '%Y-%m-%d')
    except ValueError:
        print("Error: El formato de fecha debe ser AAAA-MM-DD")
        sys.exit(1)

    monitor = Monitor(args.i, args.from_date, mongourl=args.db_url)

    try:
        monitor.scrape_gm_reviews()
        monitor.logger.info('Proceso de monitoreo completado exitosamente')
    except Exception as e:
        monitor.logger.error('Error no manejado: {}'.format(e))
        sys.exit(1)