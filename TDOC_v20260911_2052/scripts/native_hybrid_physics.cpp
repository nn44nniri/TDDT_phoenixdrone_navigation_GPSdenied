// Research-only JSBSim visible-state rollout. AI-assisted, no live plant I/O.
#include <FGFDMExec.h>
#include <initialization/FGInitialCondition.h>
#include <models/FGPropagate.h>
#include <cmath>
#include <iomanip>
#include <iostream>
#include <vector>

int main(int argc, char** argv)
{
    if (argc != 3) return 2;
    double x[13], lat, lon, alt, ground, wind[3];
    for (auto& v : x) if (!(std::cin >> v) || !std::isfinite(v)) return 3;
    if (!(std::cin >> lat >> lon >> alt >> ground >> wind[0] >> wind[1] >> wind[2])) return 3;
    size_t n, m;
    std::cin >> n;
    if (n == 0 || n > 1200) return 3;
    struct Command { double end, pwm[4]; };
    std::vector<Command> plan(n);
    for (auto& c : plan) std::cin >> c.end >> c.pwm[0] >> c.pwm[1] >> c.pwm[2] >> c.pwm[3];
    std::cin >> m;
    if (m == 0 || m > 100) return 3;
    std::vector<double> targets(m);
    for (auto& v : targets) std::cin >> v;
    if (!std::cin || targets.back() > 3) return 3;
    JSBSim::FGFDMExec fdm;
    fdm.SetRootDir(SGPath(argv[1]));
    fdm.SetAircraftPath(SGPath(argv[2]));
    fdm.SetEnginePath(SGPath(std::string(argv[1]) + "/engine"));
    fdm.SetSystemsPath(SGPath(std::string(argv[1]) + "/systems"));
    constexpr double dt = 1.0/1200.0;
    fdm.Setdt(dt);
    if (!fdm.LoadModel("phoenix_v3")) return 4;
    fdm.DisableOutput();
    auto ic = fdm.GetIC();
    constexpr double ft = 1.0/.3048, deg = 180.0/M_PI;
    ic->SetGeodLatitudeDegIC(lat + x[0]/6378137.0*deg);
    ic->SetLongitudeDegIC(lon + x[1]/(6378137.0*std::cos(lat/deg))*deg);
    ic->SetTerrainElevationFtIC(ground*ft);
    ic->SetAltitudeASLFtIC((alt-x[2])*ft);
    const double norm = std::sqrt(x[6]*x[6]+x[7]*x[7]+x[8]*x[8]+x[9]*x[9]);
    const double w=x[6]/norm, a=x[7]/norm, b=x[8]/norm, c=x[9]/norm;
    ic->SetPhiRadIC(std::atan2(2*(w*a+b*c), 1-2*(a*a+b*b)));
    ic->SetThetaRadIC(std::asin(std::fmax(-1.,std::fmin(1.,2*(w*b-c*a)))));
    ic->SetPsiRadIC(std::atan2(2*(w*c+a*b), 1-2*(b*b+c*c)));
    ic->SetVNorthFpsIC(x[3]*ft); ic->SetVEastFpsIC(x[4]*ft); ic->SetVDownFpsIC(x[5]*ft);
    ic->SetPRadpsIC(x[10]); ic->SetQRadpsIC(x[11]); ic->SetRRadpsIC(x[12]);
    ic->SetWindNEDFpsIC(wind[0]*ft,wind[1]*ft,wind[2]*ft);
    if (!fdm.RunIC()) return 5;
    double pos[3] = {x[0],x[1],x[2]}, previous[3] = {x[3],x[4],x[5]};
    size_t command = 0, sample = 0;
    const char* velocity[] = {"velocities/v-north-fps","velocities/v-east-fps","velocities/v-down-fps"};
    std::cout << std::setprecision(17);
    const int steps = std::lround(targets.back()/dt);
    for (int k=1; k<=steps; ++k) {
        const double before=(k-1)*dt;
        while (command+1<n && before >= plan[command].end-1e-9) ++command;
        const auto& u=plan[command];
        fdm.SetPropertyValue("fcs/left-elevon-cmd-norm", (u.pwm[0]-1500)/500);
        fdm.SetPropertyValue("fcs/right-elevon-cmd-norm", (u.pwm[1]-1500)/500);
        fdm.SetPropertyValue("fcs/left-motor-cmd-norm", (u.pwm[2]-1000)/1000);
        fdm.SetPropertyValue("fcs/right-motor-cmd-norm", (u.pwm[3]-1000)/1000);
        if (!fdm.Run()) return 6;
        double v[3];
        for (int i=0; i<3; ++i) {
            v[i]=fdm.GetPropertyValue(velocity[i])*.3048;
            pos[i]+=dt*(previous[i]+v[i])*.5;
            previous[i]=v[i];
        }
        if (sample<m && std::fabs(k*dt-targets[sample])<1e-9) {
            const auto q=fdm.GetPropagate()->GetQuaternion();
            std::cout << "TDDT {\"offset_s\":" << targets[sample] << ",\"plant_state\":[";
            for (int i=0; i<3; ++i) std::cout << pos[i] << ',';
            for (double value : v) std::cout << value << ',';
            for (int i=1; i<=4; ++i) std::cout << q(i) << ',';
            std::cout << fdm.GetPropertyValue("velocities/p-rad_sec") << ','
                      << fdm.GetPropertyValue("velocities/q-rad_sec") << ','
                      << fdm.GetPropertyValue("velocities/r-rad_sec") << "]}" << std::endl;
            ++sample;
        }
    }
    return sample == m ? 0 : 7;
}
