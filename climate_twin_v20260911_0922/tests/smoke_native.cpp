#include "micromet.hpp"
#include <iostream>
int main(){micromet::In i;i.lat=47.525;i.elevation=100;i.source_elevation=100;i.temperature=20;i.rh=50;i.wind_speed=5;i.wind_dir_deg=270;i.hour_local=12;i.day_of_year=215;auto o=micromet::compute(i);std::cout<<o.t<<","<<o.rh<<","<<o.wind_t<<","<<o.pressure<<"\n";return !(o.pressure>90000 && o.wind_t>0);}
